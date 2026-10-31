"""Hard-rule enforcement in src/agents/damage_assessor.py. Four rules are
code-enforced after the LLM responds, never left to the prompt alone:
  1. should_total_loss()'s computed ratio decides repair_vs_replace.
  2. total_loss always pays the ACV, never a repair-cost figure.
  3. a repair assessment deviating far from the repair-estimate document's
     real stated_total is capped back (the LLM's job is to flag a specific
     implausible line item, not invent a replacement figure - there is no
     trustworthy external "typical repair cost" source available, see this
     module's docstring).
  4. no repair-estimate document at all forces requires_physical_inspection.
"""

from types import SimpleNamespace

import src.agents.damage_assessor as da
from src.models.schemas import DamageAssessmentOutput


def _depreciation_info(acv, is_total_loss, ratio, stated_total=None):
    info = {
        "asset_age_years": 5, "estimated_acv": acv,
        "is_total_loss": is_total_loss, "total_loss_ratio": ratio,
        "depreciation_applied_usd": 0.0,
    }
    if stated_total is not None:
        info["repair_estimate_stated_total"] = stated_total
    return info


def _output(assessed, recommendation, requires_inspection=False):
    return DamageAssessmentOutput(
        assessed_damage_usd=assessed, line_items=[], repair_vs_replace=recommendation,
        assessment_confidence=0.9, assessment_notes="model notes",
        requires_physical_inspection=requires_inspection, comparable_claims_avg=None,
    )


# ── Rule 1: computed ratio decides repair_vs_replace ───────────────────────

def test_llm_says_repair_but_ratio_crosses_threshold_forces_total_loss():
    output = _output(assessed=23100.0, recommendation="repair")  # LLM disagreed with the real ratio
    dep_info = _depreciation_info(acv=13000.0, is_total_loss=True, ratio=1.777)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T1")
    assert output.repair_vs_replace == "total_loss"


def test_llm_says_total_loss_but_ratio_is_under_threshold_forces_repair():
    output = _output(assessed=13000.0, recommendation="total_loss")  # LLM over-called it
    dep_info = _depreciation_info(acv=20000.0, is_total_loss=False, ratio=0.675)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T2")
    assert output.repair_vs_replace == "repair"


# ── Rule 2: total_loss always pays the ACV ──────────────────────────────────

def test_total_loss_amount_is_forced_to_acv_even_if_llm_returned_something_else():
    output = _output(assessed=23100.0, recommendation="total_loss")  # LLM used repair cost, not ACV
    dep_info = _depreciation_info(acv=13000.0, is_total_loss=True, ratio=1.777)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T3")
    assert output.assessed_damage_usd == 13000.0


def test_rule_1_flip_to_total_loss_also_triggers_rule_2():
    """A claim that flips to total_loss via rule 1 must still get its
    dollar amount corrected to ACV by rule 2 in the same pass."""
    output = _output(assessed=23100.0, recommendation="repair")
    dep_info = _depreciation_info(acv=13000.0, is_total_loss=True, ratio=1.777)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T4")
    assert output.repair_vs_replace == "total_loss"
    assert output.assessed_damage_usd == 13000.0


# ── Rule 3: deviation cap against the repair-estimate's real stated_total ──

def test_assessed_amount_wildly_above_stated_total_is_capped():
    """The LLM discarded the shop's real $13,500 estimate and invented its
    own much higher figure - rule 3 caps it back toward the real number."""
    output = _output(assessed=40000.0, recommendation="repair")
    dep_info = _depreciation_info(acv=50000.0, is_total_loss=False, ratio=0.26, stated_total=13500.0)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T5")
    assert output.assessed_damage_usd == 13500.0 * (1 + da._MAX_DEVIATION_FROM_STATED_TOTAL)


def test_assessed_amount_within_deviation_of_stated_total_is_left_alone():
    """A modest adjustment (the LLM flagging one specific line item) stays
    within the allowed deviation and is not touched."""
    output = _output(assessed=14200.0, recommendation="repair")  # ~5% above stated_total
    dep_info = _depreciation_info(acv=50000.0, is_total_loss=False, ratio=0.28, stated_total=13500.0)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T6")
    assert output.assessed_damage_usd == 14200.0


def test_deviation_cap_does_not_apply_to_a_total_loss_payout():
    """The ACV IS the correct total-loss payout even if it's far from the
    repair estimate's stated_total - rule 3 must not re-clip what rule 2
    just set correctly (rule 2 returns before rule 3 runs)."""
    output = _output(assessed=13000.0, recommendation="total_loss")
    dep_info = _depreciation_info(acv=13000.0, is_total_loss=True, ratio=1.777, stated_total=23100.0)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T7")
    assert output.assessed_damage_usd == 13000.0


def test_no_deviation_cap_when_no_repair_document_exists():
    """Without a real repair-estimate document, there's no stated_total to
    cap against - rule 3 is a no-op (rule 4 handles this case instead)."""
    output = _output(assessed=99999.0, recommendation="repair")
    dep_info = _depreciation_info(acv=50000.0, is_total_loss=False, ratio=2.0)  # no stated_total key
    da._enforce_hard_rules(output, dep_info, has_repair_document=False, claim_id="T8")
    assert output.assessed_damage_usd == 99999.0  # untouched by rule 3


# ── Rule 4: no repair-estimate document forces physical inspection ────────

def test_no_repair_document_forces_inspection_even_if_llm_said_false():
    output = _output(assessed=10000.0, recommendation="repair", requires_inspection=False)
    dep_info = _depreciation_info(acv=50000.0, is_total_loss=False, ratio=0.2)
    da._enforce_hard_rules(output, dep_info, has_repair_document=False, claim_id="T9")
    assert output.requires_physical_inspection is True


def test_repair_document_present_does_not_force_inspection():
    output = _output(assessed=13500.0, recommendation="repair", requires_inspection=False)
    dep_info = _depreciation_info(acv=50000.0, is_total_loss=False, ratio=0.27, stated_total=13500.0)
    da._enforce_hard_rules(output, dep_info, has_repair_document=True, claim_id="T10")
    assert output.requires_physical_inspection is False


# ── Grounding: repair_estimate document overrides claimant's estimated_amount ──

def test_repair_cost_prefers_the_repair_estimate_document_over_estimated_amount():
    """estimated_amount is the claimant's SELF-REPORTED TOTAL claim, which
    can include injury/property/liability costs well beyond vehicle damage
    (verified during calibration - real dataset total_claim_amount vs
    vehicle_claim). Using it for a vehicle-specific total-loss ratio
    over-triggers total_loss - this was caught by a live test against real
    few-shot example claims, where the agent incorrectly total-lossed a
    claim that should have been a repair."""
    state = {
        "claim_documents": {"repair_estimate": {"stated_total": 13500.0}},
    }
    claim = {"estimated_amount": 18000.0}
    amount, source = da._repair_cost_estimate(state, claim)
    assert amount == 13500.0
    assert "repair_estimate" in source


def test_repair_cost_falls_back_to_estimated_amount_when_no_document():
    state = {"claim_documents": None}
    claim = {"estimated_amount": 18000.0}
    amount, source = da._repair_cost_estimate(state, claim)
    assert amount == 18000.0
    assert "estimated_amount" in source


def test_repair_cost_falls_back_when_documents_present_but_no_repair_estimate():
    state = {"claim_documents": {"police_report": {"report_number": "X"}}}
    claim = {"estimated_amount": 18000.0}
    amount, source = da._repair_cost_estimate(state, claim)
    assert amount == 18000.0


# ── Grounding: line items (None vs [] vs populated) ────────────────────────

def test_line_items_is_none_when_no_documents_at_all():
    state = {"claim_documents": None}
    assert da._repair_estimate_line_items(state) is None


def test_line_items_is_none_when_documents_exist_but_no_repair_estimate():
    state = {"claim_documents": {"police_report": {"report_number": "X"}}}
    assert da._repair_estimate_line_items(state) is None


def test_line_items_returns_the_real_items_when_present():
    items = [{"description": "Bumper", "amount": 2000.0}]
    state = {"claim_documents": {"repair_estimate": {"line_items": items, "stated_total": 2000.0}}}
    assert da._repair_estimate_line_items(state) == items


def test_line_items_is_empty_list_not_none_when_document_exists_with_no_items():
    """A present-but-empty line_items list is a confirmed fact ('checked,
    found nothing itemized'), distinct from None ('no document at all') -
    both still count as has_repair_document=True for rule 4, since
    stated_total alone is still real pricing."""
    state = {"claim_documents": {"repair_estimate": {"line_items": [], "stated_total": 2000.0}}}
    assert da._repair_estimate_line_items(state) == []


# ── Full agent: LLM-failure fallback also respects rule 1+2 ────────────────

def test_llm_failure_fallback_uses_acv_when_tool_data_already_says_total_loss(monkeypatch):
    monkeypatch.setattr(da, "detect_asset_type", lambda t: "auto")
    monkeypatch.setattr(da, "calculate_asset_age", lambda y, as_of=None: 8)
    monkeypatch.setattr(da, "calculate_vehicle_acv", lambda **k: 13000.0)
    monkeypatch.setattr(da, "should_total_loss", lambda est, acv: (True, 1.777))
    monkeypatch.setattr(da, "apply_depreciation", lambda *a, **k: (0.0, 0.0))
    monkeypatch.setattr(da, "log_agent_action", lambda **k: None)
    monkeypatch.setattr(
        da, "get_structured_llm",
        lambda schema: SimpleNamespace(invoke=lambda msgs: (_ for _ in ()).throw(RuntimeError("provider down"))),
    )
    state = {
        "claim": {"claim_id": "CLM-DA1", "incident_type": "auto_collision", "estimated_amount": 23100.0,
                  "vehicle_year": 2018, "vehicle_make": "Dodge", "vehicle_model": "RAM",
                  "incident_description": "collision", "documents": []},
        "masked_claim": {},
        "claim_documents": None,
    }
    result = da.run_damage_assessor(state)
    output = result["damage_output"]
    assert output.repair_vs_replace == "total_loss"
    assert output.assessed_damage_usd == 13000.0
    assert output.requires_physical_inspection is True  # LLM-failure path always requires it
