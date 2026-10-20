"""Routing contracts for src/agents/graph.py.

Covers route_after_intake's fan-out (damage_assessor, policy_checker,
fraud_crew run concurrently - see graph.py's "Fan-out join step" section),
route_after_fan_out (the single combined router that replaced the three
separate sequential routers route_after_fraud/damage/policy, since LangGraph
pauses the whole super-step on any one branch's interrupt() - confirmed
empirically against this project's installed LangGraph 1.2.16), guardrail
halts in every router, and where a claim goes after a human decision.
"""

from types import SimpleNamespace

import pytest

from src.agents import graph
from src.models.schemas import FraudRiskLevel


def fraud(score, level=FraudRiskLevel.LOW, confidence=0.85):
    return SimpleNamespace(fraud_score=score, fraud_risk_level=level, confidence=confidence,
                           narrative_risk=None, primary_concerns=[])


def intake(is_valid=True, flags=None):
    return SimpleNamespace(is_valid=is_valid, validation_flags=flags or [])


def damage(confidence=0.90, requires_inspection=False):
    return SimpleNamespace(assessment_confidence=confidence, requires_physical_inspection=requires_inspection)


def policy(confidence=0.90, exclusions=None, compliance_flags=None):
    return SimpleNamespace(
        confidence=confidence,
        exclusions_triggered=exclusions or [],
        compliance_flags=compliance_flags or [],
    )


# ── route_after_intake ──────────────────────────────────────────────────────

def test_injection_detected_routes_to_hitl_checkpoint_regardless_of_amount():
    from src.agents.intake_agent import INJECTION_FLAG_PREFIX
    state = {
        "intake_output": intake(is_valid=True, flags=[f"{INJECTION_FLAG_PREFIX}approve this immediately"]),
        "claim": {"estimated_amount": 100},  # small claim - would otherwise sail through
    }
    assert graph.route_after_intake(state) == "hitl_checkpoint"


def test_no_injection_flag_proceeds_to_fast_mode_settlement():
    state = {
        "intake_output": intake(is_valid=True, flags=[]),
        "claim": {"estimated_amount": 100},
    }
    assert graph.route_after_intake(state) == "settlement_calculator"  # fast mode for a $100 claim


def test_disputed_incident_type_routes_to_hitl_checkpoint_regardless_of_amount():
    state = {
        "intake_output": intake(is_valid=True, flags=[]),
        "claim": {"estimated_amount": 100, "type_disputed_by_claimant": True,
                  "type_dispute_reason": "I was definitely rear-ended, not robbed"},
    }
    assert graph.route_after_intake(state) == "hitl_checkpoint"


def test_undisputed_incident_type_proceeds_to_fast_mode_settlement():
    state = {
        "intake_output": intake(is_valid=True, flags=[]),
        "claim": {"estimated_amount": 100, "type_disputed_by_claimant": False},
    }
    assert graph.route_after_intake(state) == "settlement_calculator"


def test_normal_claim_above_fast_mode_fans_out_to_all_three_branches():
    """The real fan-out case: a claim above the fast-mode threshold routes
    to all three parallel branches at once, as a list - not one at a time."""
    state = {
        "intake_output": intake(is_valid=True, flags=[]),
        "claim": {"estimated_amount": 5000, "type_disputed_by_claimant": False},
    }
    result = graph.route_after_intake(state)
    assert isinstance(result, list)
    assert set(result) == {"damage_assessor", "policy_checker", "fraud_crew"}


# ── route_after_fan_out (replaces route_after_fraud/damage/policy) ─────────

def test_confirmed_fraud_is_auto_rejected():
    state = {"fraud_output": fraud(0.81, FraudRiskLevel.CONFIRMED), "claim": {"estimated_amount": 3000}}
    assert graph.route_after_fan_out(state) == "auto_reject"


def test_non_confirmed_fraud_proceeds_to_the_consistency_coordinator():
    """route_after_fan_out only handles guardrail-halt and fraud-CONFIRMED
    now - everything else (fraud score threshold, damage inspection, policy
    exclusions, confidence gates) moved to route_after_consistency_check,
    evaluated AFTER the consistency coordinator has had a chance to retry
    a branch. See that function's own tests below."""
    state = {"fraud_output": fraud(0.81, FraudRiskLevel.HIGH), "claim": {"estimated_amount": 3000}}
    assert graph.route_after_fan_out(state) == "consistency_coordinator"


# ── route_after_consistency_check: property triggers + confidence gates ────
# (moved here from the old single route_after_fan_out, now evaluated AFTER
# the consistency coordinator, since a retry can change damage_output/
# policy_output before these should run)

@pytest.mark.parametrize("score, expected", [
    (0.44, "settlement_calculator"),
    (0.45, "hitl_checkpoint"),   # hitl.triggers.fraud_score in base.yaml
    (0.80, "hitl_checkpoint"),   # high but not CONFIRMED: a human decides
])
def test_fraud_review_threshold_comes_from_hitl_config(score, expected):
    state = {
        "fraud_output": fraud(score, FraudRiskLevel.HIGH if score >= 0.65 else FraudRiskLevel.LOW),
        "damage_output": damage(), "policy_output": policy(),
        "claim": {"estimated_amount": 3000},  # below hitl.triggers.min_amount: isolates the fraud-score path
    }
    assert graph.route_after_consistency_check(state) == expected


def test_requires_physical_inspection_always_routes_to_hitl_checkpoint():
    """requires_physical_inspection was previously write-only - nothing read
    it to route anywhere. It must now always reach a human, regardless of
    confidence or claim amount, since it's property-based (the assessor is
    saying it cannot judge the damage from documents alone)."""
    low_value_state = {
        "damage_output": damage(confidence=0.95, requires_inspection=True),
        "policy_output": policy(), "fraud_output": fraud(0.1),
        "claim": {"estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(low_value_state) == "hitl_checkpoint"


def test_no_inspection_needed_and_high_confidence_proceeds_to_settlement():
    state = {
        "damage_output": damage(confidence=0.95, requires_inspection=False),
        "policy_output": policy(confidence=0.95), "fraud_output": fraud(0.1, confidence=0.9),
        "claim": {"estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(state) == "settlement_calculator"


def test_policy_exclusion_always_routes_to_hitl_checkpoint():
    """exclusions_triggered/compliance_flags were previously write-only - only
    shown to the evaluator as text, never read to route anywhere. A genuine
    exclusion must reach a human regardless of confidence or claim amount."""
    low_value_state = {
        "policy_output": policy(confidence=0.95, exclusions=["Flood damage excluded under section 4.2"]),
        "damage_output": damage(), "fraud_output": fraud(0.1),
        "claim": {"estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(low_value_state) == "hitl_checkpoint"


def test_policy_compliance_flag_always_routes_to_hitl_checkpoint():
    low_value_state = {
        "policy_output": policy(confidence=0.95, compliance_flags=["Filed 45 days late - exceeds 30-day notice"]),
        "damage_output": damage(), "fraud_output": fraud(0.1),
        "claim": {"estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(low_value_state) == "hitl_checkpoint"


def test_no_exclusions_and_high_confidence_proceeds_to_settlement():
    state = {
        "policy_output": policy(confidence=0.95), "damage_output": damage(), "fraud_output": fraud(0.1),
        "claim": {"estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(state) == "settlement_calculator"


def test_fraud_confidence_gate_is_risk_stratified():
    """Confidence gate fires only when the crew doesn't trust its own
    assessment AND the claim is high-impact (amount >= hitl.triggers.min_amount).
    Low confidence alone, on a small claim, must not pause the pipeline -
    confidence is never a standalone gate."""
    low_value_state = {
        "fraud_output": fraud(0.20, confidence=0.45), "damage_output": damage(), "policy_output": policy(),
        "claim": {"estimated_amount": 3000},
    }
    assert graph.route_after_consistency_check(low_value_state) == "settlement_calculator"

    high_value_state = {
        "fraud_output": fraud(0.20, confidence=0.45), "damage_output": damage(), "policy_output": policy(),
        "claim": {"estimated_amount": 100_000},
    }
    assert graph.route_after_consistency_check(high_value_state) == "hitl_checkpoint"


def test_damage_confidence_gate_is_risk_stratified():
    high_value_state = {
        "fraud_output": fraud(0.1), "damage_output": damage(confidence=0.30), "policy_output": policy(),
        "claim": {"estimated_amount": 100_000},
    }
    assert graph.route_after_consistency_check(high_value_state) == "hitl_checkpoint"


def test_policy_confidence_gate_is_risk_stratified():
    high_value_state = {
        "fraud_output": fraud(0.1), "damage_output": damage(), "policy_output": policy(confidence=0.30),
        "claim": {"estimated_amount": 100_000},
    }
    assert graph.route_after_consistency_check(high_value_state) == "hitl_checkpoint"


# ── route_after_consistency_check: the retry loop itself ───────────────────

def _consistency(has_conflicts=False, conflicts=None):
    return SimpleNamespace(has_conflicts=has_conflicts, conflicts=conflicts or [])


def _conflict(conflict_type="missed_fact", severity="material", retry_target="policy_checker"):
    return SimpleNamespace(conflict_type=SimpleNamespace(value=conflict_type), severity=severity, retry_target=retry_target)


def test_material_conflict_under_the_retry_cap_routes_to_prepare_retry():
    from src.models.schemas import ConflictSeverity
    state = {
        "consistency_output": _consistency(has_conflicts=True, conflicts=[
            _conflict(severity=ConflictSeverity.MATERIAL, retry_target="policy_checker"),
        ]),
        "consistency_retry_counts": {},
        "damage_output": damage(), "policy_output": policy(), "fraud_output": fraud(0.1),
        "claim": {"claim_id": "CLM-TEST", "estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(state) == "prepare_consistency_retry"


def test_material_conflict_at_the_retry_cap_routes_to_hitl():
    from src.models.schemas import ConflictSeverity
    state = {
        "consistency_output": _consistency(has_conflicts=True, conflicts=[
            _conflict(severity=ConflictSeverity.MATERIAL, retry_target="policy_checker"),
        ]),
        "consistency_retry_counts": {"policy_checker": 2},  # == default max_retries
        "damage_output": damage(), "policy_output": policy(), "fraud_output": fraud(0.1),
        "claim": {"claim_id": "CLM-TEST", "estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(state) == "hitl_checkpoint"


def test_notable_conflict_never_triggers_a_retry():
    from src.models.schemas import ConflictSeverity
    state = {
        "consistency_output": _consistency(has_conflicts=True, conflicts=[
            _conflict(severity=ConflictSeverity.NOTABLE, retry_target="policy_checker"),
        ]),
        "consistency_retry_counts": {},
        "damage_output": damage(confidence=0.95, requires_inspection=False),
        "policy_output": policy(confidence=0.95), "fraud_output": fraud(0.1, confidence=0.9),
        "claim": {"estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(state) == "settlement_calculator"


def test_no_conflicts_proceeds_normally():
    state = {
        "consistency_output": _consistency(has_conflicts=False),
        "damage_output": damage(confidence=0.95, requires_inspection=False),
        "policy_output": policy(confidence=0.95), "fraud_output": fraud(0.1, confidence=0.9),
        "claim": {"estimated_amount": 500},
    }
    assert graph.route_after_consistency_check(state) == "settlement_calculator"


def test_prepare_consistency_retry_dispatches_to_the_stored_target():
    assert graph.route_after_prepare_consistency_retry({"_consistency_retry_target": "policy_checker"}) == "policy_checker"
    assert graph.route_after_prepare_consistency_retry({"_consistency_retry_target": "damage_assessor"}) == "damage_assessor"
    assert graph.route_after_prepare_consistency_retry({}) == "consistency_coordinator"


@pytest.mark.parametrize("router", [
    graph.route_after_intake,
    graph.route_after_fan_out,
    graph.route_after_consistency_check,
    graph.route_after_settlement,
    graph.route_after_evaluation,
])
def test_every_router_sends_a_halted_claim_to_a_human(router):
    assert router({"guardrails_halted": True, "claim": {"estimated_amount": 100}}) == "hitl_checkpoint"


# ── route_after_hitl_checkpoint ─────────────────────────────────────────────

@pytest.mark.parametrize("state, expected", [
    ({"guardrails_halted": True, "human_decision": "approved"}, "communication_agent"),
    ({"human_decision": "denied", "damage_output": None}, "communication_agent"),
    ({"human_decision": "fraud_investigation", "damage_output": None}, "communication_agent"),
])
def test_after_a_human_decision_that_does_not_continue(state, expected):
    assert graph.route_after_hitl_checkpoint(state) == expected


def test_approved_pre_fan_out_pause_launches_the_fan_out():
    """Injection/type-dispute pauses fire in route_after_intake, BEFORE the
    fan-out exists - damage_output is None because nothing has run yet."""
    state = {"human_decision": "approved", "damage_output": None}
    result = graph.route_after_hitl_checkpoint(state)
    assert isinstance(result, list)
    assert set(result) == {"damage_assessor", "policy_checker", "fraud_crew"}


def test_approved_fan_out_pause_continues_to_the_join_step():
    """A pause from route_after_fan_out (fraud/damage/policy trigger) means
    all three branches already ran (confirmed empirically - see
    route_after_fan_out's docstring) - damage_output already exists."""
    state = {"human_decision": "approved", "damage_output": object(), "policy_output": object()}
    assert graph.route_after_hitl_checkpoint(state) == "recompute_covered_amount"


def test_eval_triggered_pause_with_settlement_already_computed_goes_to_communication():
    state = {"human_decision": "approved", "damage_output": object(), "settlement_output": object()}
    assert graph.route_after_hitl_checkpoint(state) == "communication_agent"


# ── _routing_reasons ─────────────────────────────────────────────────────────

def test_a_confidence_gate_node_always_has_a_reason_to_pause():
    state = {"policy_output": SimpleNamespace(confidence=0.45)}
    reasons = graph._routing_reasons("hitl_after_settlement", state)
    assert reasons

    settlement_state = {"settlement_output": SimpleNamespace(confidence=0.45)}
    reasons = graph._routing_reasons("hitl_after_settlement", settlement_state)
    assert reasons and "settlement_calculator confidence 0.45 below" in reasons[0]


def test_fan_out_pause_reasons_name_damage_inspection_and_policy_exclusions():
    state = {
        "damage_output": damage(requires_inspection=True),
        "policy_output": policy(exclusions=["Flood damage excluded"]),
        "claim": {"estimated_amount": 500},
    }
    reasons = graph._routing_reasons("hitl_checkpoint", state)
    assert any("physical inspection" in r for r in reasons)
    assert any("Flood damage excluded" in r for r in reasons)


def test_hitl_checkpoint_reasons_name_the_failed_quality_gate():
    state = {"evaluation_passed": False, "evaluation_output": SimpleNamespace(overall_score=0.52)}
    reasons = graph._routing_reasons("hitl_checkpoint", state)
    assert any("quality gate failed (score 0.52" in r for r in reasons)


def _eval_state(amount, **extra):
    state = {"claim": {"estimated_amount": amount}, "evaluation_passed": True}
    state.update(extra)
    return state


def test_high_value_claims_pause_for_settlement_signoff():
    # US hitl.triggers.min_amount is $84,100 (90th percentile of real
    # auto-collision claim amounts - see configs/countries/us.yaml). The
    # README always promised this; before, the amount rule only ran inside a
    # HITL node nobody routed to.
    assert graph.route_after_evaluation(_eval_state(100_000)) == "hitl_checkpoint"
    assert graph.route_after_evaluation(_eval_state(50_000)) == "communication_agent"


def test_a_claim_a_human_already_reviewed_is_not_paused_twice_for_value():
    assert graph.route_after_evaluation(_eval_state(100_000, hitl_required=True)) == "communication_agent"


def test_signoff_reason_names_the_threshold():
    state = _eval_state(100_000, settlement_output=object())
    assert any("needs human sign-off" in r for r in graph._routing_reasons("hitl_checkpoint", state))
