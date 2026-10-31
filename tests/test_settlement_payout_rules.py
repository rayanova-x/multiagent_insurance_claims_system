"""Payout arithmetic is code, not model judgment.

1. A total loss pays the ACV, which already is the depreciated value - it must not be
   depreciated a second time (found while building the golden set).
2. The existing caps only bound the payout from ABOVE. A model that under-pays, or returns 0
   while approving, used to pass through untouched; the approved amount is now set in code.
"""

from types import SimpleNamespace

import src.agents.settlement_calculator as sc
from src.models.schemas import ClaimDecision, CoverageStatus, SettlementOutput


def _run(monkeypatch, *, llm_amount, repair_vs_replace, assessed, deductible=500, limit=50_000,
         decision=ClaimDecision.APPROVED, vehicle_year=2019, incident_date="2026-03-01",
         status=CoverageStatus.COVERED):
    monkeypatch.setattr(sc, "gather_with_tools", lambda *a, **k: ("", []))
    monkeypatch.setattr(sc, "get_llm", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_agent_action", lambda **k: None)
    reply = SettlementOutput(
        decision=decision, settlement_amount_usd=llm_amount, gross_damage_usd=assessed,
        deductible_applied_usd=0, depreciation_applied_usd=0, calculation_breakdown=["model math"], confidence=0.9,
    )
    monkeypatch.setattr(sc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-P", "policy_number": "P", "incident_type": "auto_collision",
                  "estimated_amount": assessed, "incident_description": "collision", "incident_date": incident_date,
                  "vehicle_year": vehicle_year},
        "masked_claim": {"incident_type": "auto_collision", "estimated_amount": assessed, "incident_description": "collision"},
        "policy_output": SimpleNamespace(coverage_status=status, deductible_usd=deductible,
                                         covered_amount_usd=limit, exclusions_triggered=[]),
        "damage_output": SimpleNamespace(assessed_damage_usd=assessed, repair_vs_replace=repair_vs_replace),
        "fraud_output": None, "human_decision": None,
    }
    return sc.run_settlement_calculator(state)["settlement_output"]


def test_total_loss_is_not_depreciated_a_second_time(monkeypatch):
    # 2019 car, incident in 2026 -> age 7; ACV 13,000 is already depreciated.
    out = _run(monkeypatch, llm_amount=6_130, repair_vs_replace="total_loss", assessed=13_000)
    assert out.depreciation_applied_usd == 0.0
    assert out.settlement_amount_usd == 12_500.0  # ACV - deductible


def test_a_repair_is_still_depreciated(monkeypatch):
    # age 7 -> 7% x 7 years = 49% of 10,000 = 4,900; 10,000 - 4,900 - 500
    out = _run(monkeypatch, llm_amount=9_500, repair_vs_replace="repair", assessed=10_000)
    assert out.depreciation_applied_usd == 4_900.0
    assert out.settlement_amount_usd == 4_600.0


def test_an_underpaying_model_is_corrected_upward_to_the_rule_amount(monkeypatch):
    out = _run(monkeypatch, llm_amount=1_000, repair_vs_replace="repair", assessed=10_000)
    assert out.settlement_amount_usd == 4_600.0
    assert any("Payout set to" in line for line in out.calculation_breakdown)


def test_an_approval_with_a_zero_amount_is_paid_the_rule_amount(monkeypatch):
    out = _run(monkeypatch, llm_amount=0, repair_vs_replace="repair", assessed=10_000)
    assert out.settlement_amount_usd == 4_600.0


def test_the_coverage_limit_still_caps_the_rule_amount(monkeypatch):
    out = _run(monkeypatch, llm_amount=99_999, repair_vs_replace="total_loss", assessed=13_000, limit=8_000)
    assert out.settlement_amount_usd == 8_000.0


def test_a_correct_model_amount_is_left_alone(monkeypatch):
    out = _run(monkeypatch, llm_amount=4_600.0, repair_vs_replace="repair", assessed=10_000)
    assert out.settlement_amount_usd == 4_600.0
    assert not any("Payout set to" in line for line in out.calculation_breakdown)


def test_a_denial_is_not_turned_into_a_payout(monkeypatch):
    out = _run(monkeypatch, llm_amount=0, repair_vs_replace="repair", assessed=10_000, decision=ClaimDecision.DENIED)
    assert out.decision == ClaimDecision.DENIED and out.settlement_amount_usd == 0


# ── The approved / approved_partial label is derived from the payout, not picked by the model ──

def test_a_full_formula_payout_is_labeled_approved_even_if_the_model_said_partial(monkeypatch):
    out = _run(monkeypatch, llm_amount=4_600.0, repair_vs_replace="repair", assessed=10_000, decision=ClaimDecision.APPROVED_PARTIAL)
    assert out.decision == ClaimDecision.APPROVED and out.settlement_amount_usd == 4_600.0


def test_a_payout_cut_by_the_coverage_limit_is_labeled_partial(monkeypatch):
    out = _run(monkeypatch, llm_amount=4_600.0, repair_vs_replace="repair", assessed=10_000, limit=3_000)
    assert out.decision == ClaimDecision.APPROVED_PARTIAL and out.settlement_amount_usd == 3_000.0


def test_a_partially_covered_policy_makes_the_approval_partial(monkeypatch):
    out = _run(monkeypatch, llm_amount=4_600.0, repair_vs_replace="repair", assessed=10_000, status=CoverageStatus.PARTIALLY_COVERED)
    assert out.decision == ClaimDecision.APPROVED_PARTIAL


def test_the_ordinary_deductible_and_depreciation_do_not_make_an_approval_partial(monkeypatch):
    out = _run(monkeypatch, llm_amount=4_600.0, repair_vs_replace="repair", assessed=10_000, deductible=1_000)
    assert out.decision == ClaimDecision.APPROVED and out.depreciation_applied_usd > 0
