"""src/security/upload_staging.py: pre-claim document upload staging.
Path-traversal safety, slot/size/PDF validation at the trust boundary,
manifest round-trip, and claiming (move staging -> claim-scoped storage).
"""

import uuid

import pytest

import src.security.upload_staging as us


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "_STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(us, "_CLAIMED_ROOT", tmp_path / "claims")


def _token() -> str:
    return str(uuid.uuid4())


# ── Token/slot validation (trust boundary) ──────────────────────────────────

def test_non_uuid_token_is_rejected():
    with pytest.raises(ValueError):
        us.save_staged_file("../../etc/passwd", "repair_estimate", "f.pdf", b"%PDF-1.4 x")


def test_invalid_slot_is_rejected():
    with pytest.raises(ValueError):
        us.save_staged_file(_token(), "not_a_real_slot", "f.pdf", b"%PDF-1.4 x")


def test_non_pdf_content_is_rejected():
    with pytest.raises(ValueError):
        us.save_staged_file(_token(), "repair_estimate", "f.pdf", b"not a pdf at all")


def test_oversized_file_is_rejected():
    big = b"%PDF-1.4" + b"x" * (us._MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError):
        us.save_staged_file(_token(), "repair_estimate", "f.pdf", big)


# ── Save / manifest round-trip ──────────────────────────────────────────────

def test_save_and_record_round_trips_through_manifest():
    token = _token()
    path = us.save_staged_file(token, "repair_estimate", "repair.pdf", b"%PDF-1.4 fake content")
    assert path.exists()

    us.record_staged_result(token, "repair_estimate", path, status="ok", message="Uploaded.", finding=None)
    manifest = us.get_staged_manifest(token)
    assert manifest["repair_estimate"]["status"] == "ok"
    assert manifest["repair_estimate"]["path"] == str(path)


def test_reupload_overwrites_previous_slot_entry():
    token = _token()
    path1 = us.save_staged_file(token, "repair_estimate", "a.pdf", b"%PDF-1.4 first")
    us.record_staged_result(token, "repair_estimate", path1, status="ok", message="Uploaded.", finding=None)

    path2 = us.save_staged_file(token, "repair_estimate", "b.pdf", b"%PDF-1.4 second")
    us.record_staged_result(token, "repair_estimate", path2, status="problem", message="Hmm.", finding={"issue": "wrong_document_type"})

    assert path1 == path2  # same slot -> same file path, overwritten in place
    manifest = us.get_staged_manifest(token)
    assert manifest["repair_estimate"]["status"] == "problem"


def test_remove_staged_file_clears_manifest_and_deletes_file():
    token = _token()
    path = us.save_staged_file(token, "police_report", "a.pdf", b"%PDF-1.4 x")
    us.record_staged_result(token, "police_report", path, status="ok", message="Uploaded.", finding=None)

    us.remove_staged_file(token, "police_report")
    assert "police_report" not in us.get_staged_manifest(token)
    assert not path.exists()


def test_unknown_token_returns_empty_manifest():
    assert us.get_staged_manifest(_token()) == {}


# ── Claiming: staging -> claim-scoped storage ───────────────────────────────

def test_claim_staged_documents_moves_files_and_returns_uploaded_documents_shape():
    token = _token()
    for slot in ("police_report", "repair_estimate"):
        path = us.save_staged_file(token, slot, f"{slot}.pdf", b"%PDF-1.4 x")
        us.record_staged_result(token, slot, path, status="ok", message="Uploaded.", finding=None)

    result = us.claim_staged_documents(token, "CLM-TEST-001")

    assert set(result.keys()) == {"police_report", "repair_estimate"}
    for slot, entry in result.items():
        assert "CLM-TEST-001" in entry["path"]
        from pathlib import Path
        assert Path(entry["path"]).exists()

    # Staging directory is cleaned up after claiming.
    assert us.get_staged_manifest(token) == {}


def test_claim_staged_documents_excludes_other_slot_from_returned_dict():
    """'other' files are moved (so they're not lost) but never included in
    uploaded_documents, since document_agent never runs structured
    extraction on them - matches the original document-agent design."""
    token = _token()
    path = us.save_staged_file(token, "other", "misc.pdf", b"%PDF-1.4 x")
    us.record_staged_result(token, "other", path, status="ok", message="Uploaded.", finding=None)

    result = us.claim_staged_documents(token, "CLM-TEST-002")
    assert result == {}

    from pathlib import Path
    assert (us._CLAIMED_ROOT / "CLM-TEST-002" / "other.pdf").exists()


def test_claim_staged_documents_with_no_uploads_returns_empty_dict():
    assert us.claim_staged_documents(_token(), "CLM-TEST-003") == {}
