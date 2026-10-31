"""run_document_agent: extraction -> classify -> structured-extract per
upload slot, plus code-only arithmetic/plausibility verification. No real
Docling/LLM calls here - _extract_text and get_structured_llm are mocked,
same pattern as tests/test_intake_agent.py's _mock_notes_llm.
"""

from types import SimpleNamespace

import src.agents.document_agent as da
from src.models.documents import (
    DocumentClassificationOutput,
    DocumentType,
    OtherDriverInfoFields,
    PoliceReportFields,
    RepairEstimateFields,
    RepairLineItem,
)


def _claim(**overrides):
    base = {"claim_id": "CLM-D1", "estimated_amount": 23100.0}
    base.update(overrides)
    return base


def _state(uploaded_documents=None, **claim_overrides):
    return {"claim": _claim(**claim_overrides), "uploaded_documents": uploaded_documents}


def _mock_classify(monkeypatch, detected_type, confidence=0.95):
    monkeypatch.setattr(
        da, "_classify_document",
        lambda text: DocumentClassificationOutput(detected_type=detected_type, confidence=confidence, reasoning="mocked"),
    )


def _mock_extract_text(monkeypatch, text_by_path=None, default="some extracted text"):
    text_by_path = text_by_path or {}
    monkeypatch.setattr(da, "_extract_text", lambda path: text_by_path.get(path, default))


def _mock_structured_extract(monkeypatch, result):
    monkeypatch.setattr(da, "_extract_structured_fields", lambda text, schema: result)


def _uploaded(**slots):
    return {k: {"path": f"/fake/{k}.pdf"} for k in slots}


# ── No uploads: no-op pass-through ──────────────────────────────────────────

def test_no_uploaded_documents_is_a_noop(monkeypatch):
    result = da.run_document_agent(_state(uploaded_documents=None))
    assert "claim_documents" not in result
    assert result["agent_call_count"] == 0
    assert result["pipeline_trace"][0]["decision"] == "no_uploads"


# ── Clean extraction path ───────────────────────────────────────────────────

def test_clean_repair_estimate_extracts_and_computes_line_items_sum(monkeypatch, tmp_path):
    f = tmp_path / "repair.pdf"
    f.write_bytes(b"fake pdf bytes")
    _mock_extract_text(monkeypatch)
    _mock_classify(monkeypatch, DocumentType.REPAIR_ESTIMATE)
    _mock_structured_extract(monkeypatch, RepairEstimateFields(
        shop_name="Summit Auto Body",
        line_items=[RepairLineItem(description="Parts", amount="13860.00"), RepairLineItem(description="Labor", amount="9240.00")],
        line_items_sum=None,  # LLM's own echo is ignored - code must compute this
        stated_total="23100.00",
    ))
    state = _state(uploaded_documents={"repair_estimate": {"path": str(f)}})
    result = da.run_document_agent(state)

    assert result["document_verification_findings"] == []
    repair = result["claim_documents"]["repair_estimate"]
    assert float(repair["line_items_sum"]) == 23100.00


def test_all_three_slots_clean_no_findings(monkeypatch, tmp_path):
    paths = {}
    for slot in ("police_report", "other_driver_info", "repair_estimate"):
        f = tmp_path / f"{slot}.pdf"
        f.write_bytes(b"x")
        paths[slot] = {"path": str(f)}

    _mock_extract_text(monkeypatch)

    # run_document_agent processes slots in a fixed order (police_report,
    # other_driver_info, repair_estimate - see its own loop), so classify
    # calls in that same order; return the matching type each time.
    order = [DocumentType.POLICE_REPORT, DocumentType.OTHER_DRIVER_INFO, DocumentType.REPAIR_ESTIMATE]
    calls = {"n": 0}

    def fake_classify(text):
        detected = order[calls["n"]]
        calls["n"] += 1
        return DocumentClassificationOutput(detected_type=detected, confidence=0.95, reasoning="mocked")
    monkeypatch.setattr(da, "_classify_document", fake_classify)

    outputs = {
        DocumentType.POLICE_REPORT: PoliceReportFields(report_number="PR-1"),
        DocumentType.OTHER_DRIVER_INFO: OtherDriverInfoFields(driver_name="Morgan Lee"),
        DocumentType.REPAIR_ESTIMATE: RepairEstimateFields(
            line_items=[RepairLineItem(description="Parts", amount="23100.00")],
            stated_total="23100.00",
        ),
    }
    monkeypatch.setattr(da, "_extract_structured_fields", lambda text, schema: next(v for k, v in outputs.items() if da._SLOT_SCHEMA[k] is schema))

    state = _state(uploaded_documents=paths)
    result = da.run_document_agent(state)
    assert result["document_verification_findings"] == []
    assert result["claim_documents"]["police_report"]["report_number"] == "PR-1"
    assert result["claim_documents"]["other_driver_info"]["driver_name"] == "Morgan Lee"


# ── Wrong document in wrong slot ────────────────────────────────────────────

def test_wrong_document_type_in_slot_skips_extraction_and_flags(monkeypatch, tmp_path):
    f = tmp_path / "repair.pdf"
    f.write_bytes(b"x")
    _mock_extract_text(monkeypatch)
    _mock_classify(monkeypatch, DocumentType.POLICE_REPORT)  # uploaded into repair_estimate slot
    extraction_called = []
    monkeypatch.setattr(da, "_extract_structured_fields", lambda text, schema: extraction_called.append(1) or None)

    state = _state(uploaded_documents={"repair_estimate": {"path": str(f)}})
    result = da.run_document_agent(state)

    assert extraction_called == []  # extraction never ran for the mismatched slot
    assert result["claim_documents"]["repair_estimate"] is None
    findings = result["document_verification_findings"]
    assert len(findings) == 1
    assert findings[0]["issue"] == "wrong_document_type"


# ── Corrupt / unreadable file ────────────────────────────────────────────────

def test_extraction_failure_flags_could_not_parse_and_does_not_crash(monkeypatch, tmp_path):
    f = tmp_path / "corrupt.pdf"
    f.write_bytes(b"not a real pdf")
    monkeypatch.setattr(da, "_extract_text", lambda path: None)  # Docling failure

    state = _state(uploaded_documents={"repair_estimate": {"path": str(f)}})
    result = da.run_document_agent(state)

    assert result["claim_documents"]["repair_estimate"] is None
    findings = result["document_verification_findings"]
    assert len(findings) == 1
    assert findings[0]["issue"] == "could_not_parse"


def test_missing_file_path_flags_could_not_parse_without_crashing():
    state = _state(uploaded_documents={"repair_estimate": {"path": "/does/not/exist.pdf"}})
    result = da.run_document_agent(state)
    assert result["claim_documents"]["repair_estimate"] is None
    assert result["document_verification_findings"][0]["issue"] == "could_not_parse"


# ── Arithmetic mismatch ──────────────────────────────────────────────────────

def test_line_items_sum_mismatch_is_flagged_but_data_is_kept(monkeypatch, tmp_path):
    f = tmp_path / "repair.pdf"
    f.write_bytes(b"x")
    _mock_extract_text(monkeypatch)
    _mock_classify(monkeypatch, DocumentType.REPAIR_ESTIMATE)
    _mock_structured_extract(monkeypatch, RepairEstimateFields(
        line_items=[RepairLineItem(description="Parts", amount="13860.00"), RepairLineItem(description="Labor", amount="9240.00")],
        stated_total="28500.00",  # sums to 23100, document claims 28500
    ))
    state = _state(uploaded_documents={"repair_estimate": {"path": str(f)}}, estimated_amount=28500.0)
    result = da.run_document_agent(state)

    findings = result["document_verification_findings"]
    assert any(f["issue"] == "line_items_sum_mismatch" for f in findings)
    # Data is kept, not nulled, despite the flag
    assert result["claim_documents"]["repair_estimate"] is not None


# ── Plausibility mismatch ────────────────────────────────────────────────────

def test_amount_implausible_vs_claim_is_flagged(monkeypatch, tmp_path):
    f = tmp_path / "repair.pdf"
    f.write_bytes(b"x")
    _mock_extract_text(monkeypatch)
    _mock_classify(monkeypatch, DocumentType.REPAIR_ESTIMATE)
    _mock_structured_extract(monkeypatch, RepairEstimateFields(
        line_items=[RepairLineItem(description="Frame", amount="85000.00")],
        stated_total="85000.00",
    ))
    state = _state(uploaded_documents={"repair_estimate": {"path": str(f)}}, estimated_amount=23100.0)
    result = da.run_document_agent(state)

    findings = result["document_verification_findings"]
    assert any(f["issue"] == "amount_implausible_vs_claim" for f in findings)


# ── Legitimately sparse document (lump sum, no line items) ──────────────────

def test_sparse_lump_sum_document_is_not_flagged(monkeypatch, tmp_path):
    f = tmp_path / "repair.pdf"
    f.write_bytes(b"x")
    _mock_extract_text(monkeypatch)
    _mock_classify(monkeypatch, DocumentType.REPAIR_ESTIMATE)
    _mock_structured_extract(monkeypatch, RepairEstimateFields(
        line_items=[],  # confirmed zero itemized lines, not unchecked
        stated_total="23100.00",
    ))
    state = _state(uploaded_documents={"repair_estimate": {"path": str(f)}}, estimated_amount=23100.0)
    result = da.run_document_agent(state)

    assert result["document_verification_findings"] == []
    assert result["claim_documents"]["repair_estimate"]["stated_total"] == "23100.00"


# ── LLM failure fallback (classification call) ──────────────────────────────

def test_classification_llm_failure_fails_toward_caution(monkeypatch, tmp_path):
    f = tmp_path / "repair.pdf"
    f.write_bytes(b"x")
    _mock_extract_text(monkeypatch)
    monkeypatch.setattr(da, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: (_ for _ in ()).throw(RuntimeError("LLM down"))))

    state = _state(uploaded_documents={"repair_estimate": {"path": str(f)}})
    result = da.run_document_agent(state)

    # Fails toward caution: treated as a mismatch, not a clean pass.
    assert result["claim_documents"]["repair_estimate"] is None
    assert result["document_verification_findings"][0]["issue"] == "wrong_document_type"
