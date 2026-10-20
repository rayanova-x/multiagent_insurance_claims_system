"""Fast mode skips the policy checker. The settlement agent used to treat the
deductible as 0 in that case, and live runs paid a $450 claim in full on a
policy with a $2,500 deductible. The deductible now comes from the policy
record, and the payout is capped in code, not only in the prompt.
"""

from types import SimpleNamespace

import src.agents.settlement_calculator as sc
from src.models.schemas import ClaimDecision, FraudRiskLevel, SettlementOutput


def _run(monkeypatch, llm_amount, deductible, estimated, human_decision=None):
    monkeypatch.setattr(sc, "lookup_policy", lambda n: {"policy_number": n})
    monkeypatch.setattr(sc, "get_coverage_for_claim_type",
                        lambda p, t: {"deductible": deductible, "coverage_limit": 100_000})
    monkeypatch.setattr(sc, "gather_with_tools", lambda *a, **k: ("", []))
    monkeypatch.setattr(sc, "get_llm", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_agent_action", lambda **k: None)
    reply = SettlementOutput(
        decision=ClaimDecision.APPROVED, settlement_amount_usd=llm_amount, gross_damage_usd=estimated,
        deductible_applied_usd=0, depreciation_applied_usd=0, calculation_breakdown=["model math"],
        confidence=0.9,
    )
    monkeypatch.setattr(sc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-D", "policy_number": "POL-HOME", "incident_type": "property_water",
                  "estimated_amount": estimated, "incident_description": "leak"},
        "masked_claim": {"incident_type": "property_water", "estimated_amount": estimated,
                         "incident_description": "leak"},
        "policy_output": None, "damage_output": None, "fraud_output": None,
        "human_decision": human_decision,
    }
    return sc.run_settlement_calculator(state)["settlement_output"]


def test_claim_below_its_deductible_is_denied_in_fast_mode(monkeypatch):
    out = _run(monkeypatch, llm_amount=450, deductible=2500, estimated=450)
    assert out.decision == ClaimDecision.DENIED
    assert out.settlement_amount_usd == 0
    assert "Deductible" in out.denial_reasons[0]


def test_payout_is_capped_at_damage_minus_deductible(monkeypatch):
    out = _run(monkeypatch, llm_amount=480, deductible=100, estimated=480)
    assert out.settlement_amount_usd == 380


def test_a_reviewer_approval_is_not_overturned_by_a_fraud_investigation_decision(monkeypatch):
    # Live run: India path B was approved by the reviewer, then the settlement
    # model returned fraud_investigation and the claim ended there.
    monkeypatch.setattr(sc, "lookup_policy", lambda n: {"policy_number": n})
    monkeypatch.setattr(sc, "get_coverage_for_claim_type",
                        lambda p, t: {"deductible": 1000, "coverage_limit": 100_000})
    monkeypatch.setattr(sc, "gather_with_tools", lambda *a, **k: ("", []))
    monkeypatch.setattr(sc, "get_llm", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_agent_action", lambda **k: None)
    reply = SettlementOutput(
        decision=ClaimDecision.FRAUD_INVESTIGATION, settlement_amount_usd=0, gross_damage_usd=20_000,
        deductible_applied_usd=0, depreciation_applied_usd=0, calculation_breakdown=["held"],
        confidence=0.9,
    )
    monkeypatch.setattr(sc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-H", "policy_number": "P", "incident_type": "theft",
                  "estimated_amount": 20_000, "incident_description": "stolen"},
        "masked_claim": {"incident_type": "theft", "estimated_amount": 20_000, "incident_description": "stolen"},
        "policy_output": None, "damage_output": None, "fraud_output": None,
        "human_decision": "approved",
    }
    out = sc.run_settlement_calculator(state)["settlement_output"]
    assert out.decision == ClaimDecision.APPROVED
    assert out.settlement_amount_usd > 0
    assert "fraud_investigation" in out.calculation_breakdown[-1]


def test_a_reviewer_settlement_override_survives_the_settlement_agent(monkeypatch):
    # Fraud-triggered pause, reviewer approves with an amount, pipeline then
    # runs settlement. The reviewer's amount used to be replaced.
    monkeypatch.setattr(sc, "lookup_policy", lambda n: {"policy_number": n})
    monkeypatch.setattr(sc, "get_coverage_for_claim_type",
                        lambda p, t: {"deductible": 500, "coverage_limit": 100_000})
    monkeypatch.setattr(sc, "gather_with_tools", lambda *a, **k: ("", []))
    monkeypatch.setattr(sc, "get_llm", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_agent_action", lambda **k: None)
    reply = SettlementOutput(
        decision=ClaimDecision.APPROVED, settlement_amount_usd=9_000, gross_damage_usd=10_000,
        deductible_applied_usd=500, depreciation_applied_usd=500, calculation_breakdown=["model"],
        confidence=0.9,
    )
    monkeypatch.setattr(sc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-O", "policy_number": "P", "incident_type": "auto_collision",
                  "estimated_amount": 10_000, "incident_description": "x"},
        "masked_claim": {"incident_type": "auto_collision", "estimated_amount": 10_000, "incident_description": "x"},
        "policy_output": None, "damage_output": None, "fraud_output": None,
        "human_decision": "approved", "human_settlement_override_usd": 7_250.0,
    }
    result = sc.run_settlement_calculator(state)
    assert result["final_amount_usd"] == 7_250.0
    assert "Reviewer set the settlement" in result["settlement_output"].calculation_breakdown[-1]


def _fraud(score, risk=FraudRiskLevel.HIGH):
    return SimpleNamespace(fraud_score=score, fraud_risk_level=risk, recommendation="proceed")


def test_high_fraud_score_denies_in_code_even_if_the_llm_approved(monkeypatch):
    """Rule 6 in the settlement prompt ("fraud score >= 0.80 -> DENIED") was
    only a prompt instruction - unlike the deductible/multiplier caps, the
    LLM's decision was trusted outright. A claim can reach settlement with a
    high fraud score if a human already approved it past the fraud HITL
    checkpoint (hitl.triggers.fraud_score=0.45); this test covers the case
    where no human has weighed in yet and the LLM still approved anyway."""
    monkeypatch.setattr(sc, "lookup_policy", lambda n: {"policy_number": n})
    monkeypatch.setattr(sc, "get_coverage_for_claim_type",
                        lambda p, t: {"deductible": 100, "coverage_limit": 100_000})
    monkeypatch.setattr(sc, "gather_with_tools", lambda *a, **k: ("", []))
    monkeypatch.setattr(sc, "get_llm", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_agent_action", lambda **k: None)
    reply = SettlementOutput(
        decision=ClaimDecision.APPROVED, settlement_amount_usd=5_000, gross_damage_usd=5_000,
        deductible_applied_usd=100, depreciation_applied_usd=0, calculation_breakdown=["model approved"],
        confidence=0.9,
    )
    monkeypatch.setattr(sc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-FRAUD", "policy_number": "P", "incident_type": "auto_collision",
                  "estimated_amount": 5_000, "incident_description": "x"},
        "masked_claim": {"incident_type": "auto_collision", "estimated_amount": 5_000, "incident_description": "x"},
        "policy_output": None, "damage_output": None, "fraud_output": _fraud(0.85),
        "human_decision": None,
    }
    out = sc.run_settlement_calculator(state)["settlement_output"]
    assert out.decision == ClaimDecision.DENIED
    assert out.settlement_amount_usd == 0
    assert "Fraud score" in out.denial_reasons[0]


def test_breakdown_fields_are_overwritten_with_the_real_computed_values(monkeypatch):
    """gross_damage_usd, deductible_applied_usd, depreciation_applied_usd are
    facts code already computed (assessed_damage, deductible, depreciation),
    not LLM judgment calls - the LLM's echoed-back numbers must never
    disagree with the real math, even though they don't gate anything by
    themselves (unlike settlement_amount_usd, which is also capped)."""
    monkeypatch.setattr(sc, "lookup_policy", lambda n: {"policy_number": n})
    monkeypatch.setattr(sc, "get_coverage_for_claim_type",
                        lambda p, t: {"deductible": 500, "coverage_limit": 100_000})
    monkeypatch.setattr(sc, "gather_with_tools", lambda *a, **k: ("", []))
    monkeypatch.setattr(sc, "get_llm", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_agent_action", lambda **k: None)
    # LLM echoes back wrong figures for the breakdown fields despite being
    # given the real assessed_damage/deductible/depreciation in the prompt.
    reply = SettlementOutput(
        decision=ClaimDecision.APPROVED, settlement_amount_usd=9_000,
        gross_damage_usd=123_456, deductible_applied_usd=999, depreciation_applied_usd=777,
        calculation_breakdown=["model math"], confidence=0.9,
    )
    monkeypatch.setattr(sc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-BD", "policy_number": "P", "incident_type": "property_water",
                  "estimated_amount": 10_000, "incident_description": "leak"},
        "masked_claim": {"incident_type": "property_water", "estimated_amount": 10_000,
                         "incident_description": "leak"},
        "policy_output": None, "damage_output": None, "fraud_output": None,
        "human_decision": None,
    }
    out = sc.run_settlement_calculator(state)["settlement_output"]
    assert out.gross_damage_usd == 10_000.0
    assert out.deductible_applied_usd == 500.0
    assert out.depreciation_applied_usd == 0.0  # property_water with no vehicle_year -> 0 depreciation


def test_human_approval_is_respected_even_with_a_high_fraud_score(monkeypatch):
    """A human reviewer who already saw the fraud score at the fraud HITL
    checkpoint and approved anyway has the last word - the code-enforced
    fraud denial must not re-fight a decision the human already made."""
    monkeypatch.setattr(sc, "lookup_policy", lambda n: {"policy_number": n})
    monkeypatch.setattr(sc, "get_coverage_for_claim_type",
                        lambda p, t: {"deductible": 100, "coverage_limit": 100_000})
    monkeypatch.setattr(sc, "gather_with_tools", lambda *a, **k: ("", []))
    monkeypatch.setattr(sc, "get_llm", lambda *a, **k: None)
    monkeypatch.setattr(sc, "log_agent_action", lambda **k: None)
    reply = SettlementOutput(
        decision=ClaimDecision.APPROVED, settlement_amount_usd=5_000, gross_damage_usd=5_000,
        deductible_applied_usd=100, depreciation_applied_usd=0, calculation_breakdown=["model approved"],
        confidence=0.9,
    )
    monkeypatch.setattr(sc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-FRAUD-H", "policy_number": "P", "incident_type": "auto_collision",
                  "estimated_amount": 5_000, "incident_description": "x"},
        "masked_claim": {"incident_type": "auto_collision", "estimated_amount": 5_000, "incident_description": "x"},
        "policy_output": None, "damage_output": None, "fraud_output": _fraud(0.85),
        "human_decision": "approved",
    }
    out = sc.run_settlement_calculator(state)["settlement_output"]
    assert out.decision == ClaimDecision.APPROVED
    assert out.settlement_amount_usd > 0
