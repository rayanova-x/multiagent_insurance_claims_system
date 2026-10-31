"""End-to-end architectural scenario coverage for the Smart Claims pipeline.

Unlike tests/test_graph_routing.py (which unit-tests each router FUNCTION
in isolation with hand-built state), every test here drives the REAL
compiled LangGraph graph - src.agents.graph.process_claim / resume_claim -
with every LLM-calling agent mocked via tests/_scenario_harness.py. This
proves the actual WIRING: LangGraph's real fan-out, concurrent state-merge,
and retry-loop engine, not just what each router function would return
given a hand-built state dict.

This is the layer that caught a real bug this session (the
consistency_retry_context concurrent-write InvalidUpdateError) that
isolated unit tests of the same node functions did not catch - unit tests
never exercise LangGraph's actual concurrent super-step merge.

One test per distinct routing branch in src/agents/graph.py (31 total,
enumerated against every router function's every return path - see the
project's CLAUDE.md rebuild-status log and project memory for how this
list was derived). Organized by router, in the order the pipeline reaches
them: route_after_intake -> route_after_fan_out -> route_after_consistency_check
-> route_after_settlement -> route_after_evaluation -> route_after_hitl_checkpoint
-> guardrails (cross-cutting).
"""

from __future__ import annotations

from src.config import get_hitl_config
from src.models.schemas import (
    ClaimDecision,
    ClaimType,
    ConflictSeverity,
    ConflictType,
    CoverageStatus,
    FraudRiskLevel,
)

# Real US min_amount (configs/countries/us.yaml overrides base.yaml's
# $10,000 default to $84,100 - recalibrated against real claim data earlier
# this project, see project memory). Read live, not hardcoded, so these
# tests can't silently drift out of sync with config again.
_MIN_AMOUNT = float(get_hitl_config()["triggers"]["min_amount"])
_HIGH_VALUE_AMOUNT = _MIN_AMOUNT + 5000
_LOW_VALUE_AMOUNT = _MIN_AMOUNT - 5000
from tests._scenario_harness import (
    ScenarioHarness,
    make_claim,
    make_conflict,
    make_consistency,
    make_damage,
    make_evaluation,
    make_fraud,
    make_intake,
    make_policy,
    make_settlement,
    resume_scenario,
    run_scenario,
    trace_agents,
)


# ── route_after_intake (branches 1-6) ───────────────────────────────────────

def test_01_invalid_claim_routes_straight_to_denial_communication():
    """Branch: intake invalid -> communication_agent, no other agent runs."""
    h = ScenarioHarness(intake=make_intake(is_valid=False))
    result = run_scenario(h, make_claim("CLM-SC-01"))
    assert result["paused"] is False
    assert trace_agents(result["state"]) == ["document_agent", "intake_agent", "communication_agent"]
    assert h.calls["damage"] == 0 and h.calls["policy"] == 0 and h.calls["fraud"] == 0


def test_02_prompt_injection_flag_pauses_for_human_review():
    """Branch: injection flag at intake -> hitl_checkpoint, regardless of amount."""
    from src.agents.intake_agent import INJECTION_FLAG_PREFIX
    h = ScenarioHarness(intake=make_intake(flags=[f"{INJECTION_FLAG_PREFIX}ignore prior instructions"]))
    result = run_scenario(h, make_claim("CLM-SC-02", estimated_amount=100))
    assert result["paused"] is True
    assert h.calls["damage"] == 0  # paused before the fan-out ever starts


def test_03_disputed_incident_type_pauses_for_human_review():
    """Branch: claimant disputed the suggested incident_type -> hitl_checkpoint."""
    h = ScenarioHarness()
    claim = make_claim("CLM-SC-03", estimated_amount=100,
                        type_disputed_by_claimant=True, type_dispute_reason="I disagree")
    result = run_scenario(h, claim)
    assert result["paused"] is True
    assert h.calls["damage"] == 0


def test_04_intake_confidence_gate_is_inert_no_confidence_field():
    """Branch: intake confidence gate is dead code (IntakeValidationOutput has
    no confidence field - getattr degrades to None, gate never fires)."""
    h = ScenarioHarness(intake=make_intake())
    result = run_scenario(h, make_claim("CLM-SC-04", estimated_amount=100))
    assert result["paused"] is False  # fast-mode settlement, not hitl_after_intake


def test_05_fast_mode_small_claim_skips_the_fanout_entirely():
    """Branch: amount < pipeline.fast_mode.max_amount ($500) -> settlement
    directly, damage/policy/fraud never run."""
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-SC-05", estimated_amount=100))
    assert result["paused"] is False
    assert trace_agents(result["state"]) == [
        "document_agent", "intake_agent", "settlement_calculator", "evaluator", "communication_agent",
    ]
    assert h.calls["damage"] == 0 and h.calls["policy"] == 0 and h.calls["fraud"] == 0


def test_06_normal_valid_claim_fans_out_to_all_three_branches():
    """Branch: valid claim above fast-mode -> damage/policy/fraud all run,
    in the SAME LangGraph super-step (real concurrency, not sequential)."""
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-SC-06"))
    assert h.calls["damage"] == 1 and h.calls["policy"] == 1 and h.calls["fraud"] == 1
    trace = trace_agents(result["state"])
    assert set(trace[2:5]) == {"damage_assessor", "policy_checker", "fraud_crew"}


# ── route_after_fan_out (branches 7-9) ──────────────────────────────────────

def test_07_guardrail_halt_during_fanout_pauses_for_human_review():
    """Branch: guardrails_halted after the fan-out -> hitl_checkpoint, before
    the consistency coordinator ever runs."""
    h = ScenarioHarness(guardrail_halt_after=3)  # trips partway through the fan-out
    result = run_scenario(h, make_claim("CLM-SC-07"))
    assert result["paused"] is True
    assert h.calls["consistency"] == 0


def test_08_confirmed_fraud_auto_rejects_skipping_consistency_check():
    """Branch: fraud_risk_level == CONFIRMED -> auto_reject -> communication,
    never reaches consistency_coordinator (a confirmed-fraud claim doesn't
    need cross-branch consistency checking)."""
    h = ScenarioHarness(fraud=make_fraud(score=0.95, level=FraudRiskLevel.CONFIRMED))
    result = run_scenario(h, make_claim("CLM-SC-08"))
    assert result["paused"] is False
    assert h.calls["consistency"] == 0
    assert "communication_agent" in trace_agents(result["state"])


def test_09_non_confirmed_fraud_proceeds_to_consistency_coordinator():
    """Branch: anything other than CONFIRMED fraud -> consistency_coordinator runs."""
    h = ScenarioHarness(fraud=make_fraud(score=0.3, level=FraudRiskLevel.MEDIUM))
    result = run_scenario(h, make_claim("CLM-SC-09"))
    assert h.calls["consistency"] >= 1


# ── route_after_consistency_check: retry loop (branches 10-12) ─────────────

def test_10_material_conflict_under_cap_retries_the_target_agent():
    """Branch: MATERIAL conflict, target agent under max_retries -> loops
    back to that agent WITH the conflict as added context, then rechecks."""
    conflict = make_conflict(conflict_type=ConflictType.SEVERITY_MISMATCH,
                              severity=ConflictSeverity.MATERIAL, retry_target="damage_assessor")
    h = ScenarioHarness(consistency_sequence=[
        make_consistency(has_conflicts=True, conflicts=[conflict]),
        make_consistency(has_conflicts=False),  # resolved after the retry
    ])
    result = run_scenario(h, make_claim("CLM-SC-10"))
    assert result["paused"] is False
    assert h.calls["damage"] == 2  # original pass + one retry
    assert h.calls["consistency"] == 2
    assert "retry_ctx_seen" in result["state"]["damage_output"].assessment_notes


def test_11_material_conflict_at_retry_cap_forces_hitl():
    """Branch: MATERIAL conflict still unresolved after max_retries (2) ->
    forced to hitl_checkpoint regardless of resolution, loop never infinite."""
    conflict = make_conflict(conflict_type=ConflictType.MISSED_FACT,
                              severity=ConflictSeverity.MATERIAL, retry_target="policy_checker")
    # Same unresolved conflict every single time - never resolves.
    h = ScenarioHarness(consistency_sequence=[make_consistency(has_conflicts=True, conflicts=[conflict])])
    result = run_scenario(h, make_claim("CLM-SC-11"))
    assert result["paused"] is True
    assert h.calls["policy"] == 3  # original + 2 retries (max_retries=2), then HITL
    assert h.calls["consistency"] == 3


def test_12_notable_conflict_never_triggers_a_retry():
    """Branch: NOTABLE-severity conflict is surfaced but never routes to a retry."""
    conflict = make_conflict(severity=ConflictSeverity.NOTABLE, retry_target="damage_assessor")
    h = ScenarioHarness(consistency=make_consistency(has_conflicts=True, conflicts=[conflict]))
    result = run_scenario(h, make_claim("CLM-SC-12"))
    assert result["paused"] is False
    assert h.calls["damage"] == 1  # no retry
    assert h.calls["consistency"] == 1


# ── route_after_consistency_check: property triggers (branches 13-15) ──────

def test_13_fraud_score_at_review_threshold_pauses_for_human_review():
    """Branch: fraud_score >= hitl.triggers.fraud_score (0.45) -> hitl_checkpoint."""
    h = ScenarioHarness(fraud=make_fraud(score=0.50, level=FraudRiskLevel.MEDIUM))
    result = run_scenario(h, make_claim("CLM-SC-13"))
    assert result["paused"] is True


def test_14_damage_requires_physical_inspection_always_pauses():
    """Branch: damage_output.requires_physical_inspection -> hitl_checkpoint,
    regardless of confidence or amount."""
    h = ScenarioHarness(damage=make_damage(requires_inspection=True, confidence=0.99))
    result = run_scenario(h, make_claim("CLM-SC-14"))
    assert result["paused"] is True


def test_15_policy_exclusion_or_compliance_flag_always_pauses():
    """Branch: policy_output has exclusions_triggered or compliance_flags -> hitl_checkpoint."""
    h = ScenarioHarness(policy=make_policy(exclusions=["racing"]))
    result = run_scenario(h, make_claim("CLM-SC-15"))
    assert result["paused"] is True


# ── route_after_consistency_check: confidence gates, risk-stratified (16, 30, 31) ─

def test_16_low_confidence_on_high_value_claim_pauses():
    """Branch 16/31: confidence below per-agent threshold AND amount >=
    hitl.triggers.min_amount (real US value, read live from config) ->
    hitl_checkpoint (risk-stratified gate fires)."""
    h = ScenarioHarness(damage=make_damage(confidence=0.30))  # well below 0.60 threshold
    result = run_scenario(h, make_claim("CLM-SC-16", estimated_amount=_HIGH_VALUE_AMOUNT))
    assert result["paused"] is True


def test_30_low_confidence_on_low_value_claim_does_not_pause():
    """Branch 30: same low confidence, but amount < min_amount -> gate does
    NOT fire (risk-stratified: low confidence alone never pauses a cheap claim)."""
    h = ScenarioHarness(damage=make_damage(confidence=0.30))
    result = run_scenario(h, make_claim("CLM-SC-30", estimated_amount=_LOW_VALUE_AMOUNT))
    assert result["paused"] is False


def test_17_clean_fanout_proceeds_straight_to_settlement():
    """Branch: no conflicts, no triggers, no low confidence -> settlement_calculator."""
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-SC-17"))
    assert result["paused"] is False
    assert "settlement_calculator" in trace_agents(result["state"])


# ── route_after_settlement (branch 18-19) ───────────────────────────────────

def test_18_low_settlement_confidence_on_high_value_claim_pauses():
    """Branch: settlement_calculator confidence gate, risk-stratified like the others."""
    h = ScenarioHarness(settlement=make_settlement(confidence=0.20))
    result = run_scenario(h, make_claim("CLM-SC-18", estimated_amount=_HIGH_VALUE_AMOUNT))
    assert result["paused"] is True
    assert h.calls["evaluator"] == 0  # paused before the evaluator runs


def test_19_normal_settlement_confidence_proceeds_to_evaluator():
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-SC-19"))
    assert h.calls["evaluator"] == 1


# ── route_after_evaluation (branches 20-23) ─────────────────────────────────

def test_20_failed_quality_gate_pauses_for_human_review():
    """Branch: evaluation_passed is explicitly False -> hitl_checkpoint before release."""
    h = ScenarioHarness(evaluation=make_evaluation(passed=False, overall_score=0.40))
    result = run_scenario(h, make_claim("CLM-SC-20"))
    assert result["paused"] is True
    assert h.calls["communication"] == 0


def test_21_high_value_claim_pauses_for_settlement_signoff():
    """Branch: amount >= hitl.triggers.min_amount needs human sign-off on the
    settlement, even when evaluation passed and nothing else triggered."""
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-SC-21", estimated_amount=_HIGH_VALUE_AMOUNT))
    assert result["paused"] is True


def test_22_skipped_evaluation_does_not_block_release():
    """Branch: evaluation_passed is None (sampled out, never judged) -> the
    claim still continues; only an explicit False diverts to a human."""
    h = ScenarioHarness(evaluation=None, )  # harness treats None as "skipped"
    result = run_scenario(h, make_claim("CLM-SC-22", estimated_amount=5000))
    assert result["paused"] is False
    assert "communication_agent" in trace_agents(result["state"])


def test_23_passed_low_value_claim_proceeds_to_communication():
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-SC-23", estimated_amount=5000))
    assert result["paused"] is False
    assert trace_agents(result["state"])[-1] == "communication_agent"


# ── route_after_hitl_checkpoint resume paths (branches 24-28) ──────────────

def test_24_resume_after_guardrail_halt_goes_straight_to_communication():
    """Branch: resuming a guardrail-halted claim -> communication_agent, no
    further agent spend regardless of the human's decision."""
    h = ScenarioHarness(guardrail_halt_after=0)
    paused = run_scenario(h, make_claim("CLM-SC-24"))
    assert paused["paused"] is True
    resumed = resume_scenario(h, "CLM-SC-24", {"decision": "approved", "reviewer_id": "r1", "notes": "", "override_ai": False})
    assert resumed["paused"] is False
    assert trace_agents(resumed["state"])[-1] == "communication_agent"


def test_25_reviewer_denies_goes_straight_to_communication():
    """Branch: human_decision not in the continue-set (denied/escalated/etc.)
    -> nothing left to calculate, straight to communication."""
    h = ScenarioHarness(fraud=make_fraud(score=0.9, level=FraudRiskLevel.CONFIRMED))
    # CONFIRMED fraud auto-rejects without pausing, so instead force a pause
    # via a policy exclusion, then have the reviewer deny it.
    h = ScenarioHarness(policy=make_policy(exclusions=["racing"]))
    paused = run_scenario(h, make_claim("CLM-SC-25"))
    assert paused["paused"] is True
    resumed = resume_scenario(h, "CLM-SC-25", {"decision": "denied", "reviewer_id": "r1", "notes": "no", "override_ai": False})
    assert resumed["paused"] is False
    assert h.calls["settlement"] == 0  # never reaches settlement


def test_26_eval_triggered_pause_approved_goes_to_communication():
    """Branch: resuming a pause that happened AFTER settlement (eval gate or
    high-value signoff) -> straight to communication, nothing recomputed."""
    h = ScenarioHarness()
    paused = run_scenario(h, make_claim("CLM-SC-26", estimated_amount=_HIGH_VALUE_AMOUNT))
    assert paused["paused"] is True
    assert h.calls["settlement"] == 1
    resumed = resume_scenario(h, "CLM-SC-26", {"decision": "approved", "reviewer_id": "r1", "notes": "", "override_ai": False})
    assert resumed["paused"] is False
    assert h.calls["settlement"] == 1  # not recomputed on resume
    assert trace_agents(resumed["state"])[-1] == "communication_agent"


def test_27_pre_fanout_pause_approved_launches_the_fanout():
    """Branch: resuming a pause that happened BEFORE the fan-out (injection/
    dispute) -> launches damage/policy/fraud now, since they never ran."""
    from src.agents.intake_agent import INJECTION_FLAG_PREFIX
    h = ScenarioHarness(intake=make_intake(flags=[f"{INJECTION_FLAG_PREFIX}x"]))
    paused = run_scenario(h, make_claim("CLM-SC-27"))
    assert paused["paused"] is True
    assert h.calls["damage"] == 0
    resumed = resume_scenario(h, "CLM-SC-27", {"decision": "approved", "reviewer_id": "r1", "notes": "", "override_ai": False})
    assert h.calls["damage"] == 1 and h.calls["policy"] == 1 and h.calls["fraud"] == 1
    assert resumed["paused"] is False


def test_28_fanout_pause_approved_continues_to_settlement_join():
    """Branch: resuming a pause that happened AFTER the fan-out (property
    trigger/confidence gate) -> continues straight to the settlement join,
    the fan-out is NOT re-run (all three branches already completed)."""
    h = ScenarioHarness(damage=make_damage(requires_inspection=True))
    paused = run_scenario(h, make_claim("CLM-SC-28"))
    assert paused["paused"] is True
    assert h.calls["damage"] == 1
    resumed = resume_scenario(h, "CLM-SC-28", {"decision": "approved", "reviewer_id": "r1", "notes": "", "override_ai": False})
    assert h.calls["damage"] == 1  # not re-run
    assert h.calls["settlement"] == 1
    assert resumed["paused"] is False


# ── Guardrails (branch 29, cross-cutting) ───────────────────────────────────

def test_29_guardrail_halt_from_any_node_routes_to_human_review():
    """Branch: a guardrail halt can fire from ANY node (budget/timeout), not
    just during the fan-out - verified here firing during a sequential node
    (settlement itself trips the halt) on a fast-mode claim, a different
    code path than test_07 (which trips the halt during the fan-out)."""
    h = ScenarioHarness(guardrail_halt_after=1)  # intake (call 1) is clean; settlement (call 2) trips it
    result = run_scenario(h, make_claim("CLM-SC-29", estimated_amount=100))
    assert result["paused"] is True
    assert h.calls["settlement"] == 1  # the halt is discovered DURING settlement's own run, not before it
    assert h.calls["evaluator"] == 0  # but it does stop the pipeline from continuing past that point
