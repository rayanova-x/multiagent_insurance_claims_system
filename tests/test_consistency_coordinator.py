"""src/agents/consistency_coordinator.py and graph.py's
prepare_consistency_retry_node: the retry-context preparation step for a
MATERIAL consistency conflict.
"""

from types import SimpleNamespace

import src.agents.consistency_coordinator as cc
from src.agents import graph
from src.models.schemas import ConflictSeverity, ConflictType, ConsistencyCheckOutput, ConflictFinding


def _damage(amount=9000.0, recommendation="repair", notes="ok"):
    return SimpleNamespace(
        assessed_damage_usd=amount, repair_vs_replace=recommendation, assessment_notes=notes,
        requires_physical_inspection=False, line_items=[],
    )


def _policy():
    return SimpleNamespace(coverage_status=SimpleNamespace(value="covered"), exclusions_triggered=[], coverage_notes="ok")


def _fraud(summary="minor fender bender"):
    return SimpleNamespace(fraud_risk_level=SimpleNamespace(value="low"), fraud_score=0.1, crew_summary=summary)


# ── run_consistency_coordinator ─────────────────────────────────────────────

def test_llm_failure_fails_open_no_conflicts(monkeypatch):
    """Unlike injection_check's fail-closed, a failed consistency check
    should not itself force a retry loop - there's no finding to act on,
    and no safety concern in letting the claim proceed without a
    consistency opinion for one run."""
    monkeypatch.setattr(cc, "get_structured_llm", lambda schema: SimpleNamespace(
        invoke=lambda msgs: (_ for _ in ()).throw(RuntimeError("provider down"))
    ))
    monkeypatch.setattr(cc, "log_agent_action", lambda **k: None)
    state = {
        "claim": {"claim_id": "CLM-CC1"}, "damage_output": _damage(), "policy_output": _policy(),
        "fraud_output": _fraud(),
    }
    result = cc.run_consistency_coordinator(state)
    output = result["consistency_output"]
    assert output.has_conflicts is False
    assert output.conflicts == []


def test_conflicts_are_surfaced_in_the_trace(monkeypatch):
    conflict = ConflictFinding(
        conflict_type=ConflictType.SEVERITY_MISMATCH, severity=ConflictSeverity.MATERIAL,
        description="damage says total_loss, fraud narrative says minor damage",
        reasoning="could change the payout", retry_target="damage_assessor",
    )
    reply = ConsistencyCheckOutput(has_conflicts=True, conflicts=[conflict], summary="found 1 conflict")
    monkeypatch.setattr(cc, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    monkeypatch.setattr(cc, "log_agent_action", lambda **k: None)
    state = {
        "claim": {"claim_id": "CLM-CC2"}, "damage_output": _damage(), "policy_output": _policy(),
        "fraud_output": _fraud(),
    }
    result = cc.run_consistency_coordinator(state)
    trace = result["pipeline_trace"][0]
    assert trace["has_conflicts"] is True
    assert "severity_mismatch" in trace["flags"][0]
    assert result["agent_call_count"] == 1


# ── prepare_consistency_retry_node ──────────────────────────────────────────

def _consistency(has_conflicts=True, conflicts=None):
    return SimpleNamespace(has_conflicts=has_conflicts, conflicts=conflicts or [])


def _conflict(severity=ConflictSeverity.MATERIAL, retry_target="policy_checker", description="d", reasoning="r"):
    return SimpleNamespace(
        conflict_type=SimpleNamespace(value="missed_fact"), severity=severity, retry_target=retry_target,
        description=description, reasoning=reasoning,
    )


def test_prepare_retry_increments_the_count_and_sets_context():
    state = {
        "consistency_output": _consistency(conflicts=[_conflict(retry_target="policy_checker")]),
        "consistency_retry_counts": {},
    }
    result = graph.prepare_consistency_retry_node(state)
    assert result["consistency_retry_counts"] == {"policy_checker": 1}
    assert "d" in result["consistency_retry_context"]  # the conflict's own description text
    assert result["_consistency_retry_target"] == "policy_checker"


def test_prepare_retry_accumulates_across_calls():
    state = {
        "consistency_output": _consistency(conflicts=[_conflict(retry_target="damage_assessor")]),
        "consistency_retry_counts": {"damage_assessor": 1, "policy_checker": 2},
    }
    result = graph.prepare_consistency_retry_node(state)
    assert result["consistency_retry_counts"] == {"damage_assessor": 2, "policy_checker": 2}


def test_prepare_retry_skips_notable_conflicts():
    state = {
        "consistency_output": _consistency(conflicts=[_conflict(severity=ConflictSeverity.NOTABLE)]),
        "consistency_retry_counts": {},
    }
    result = graph.prepare_consistency_retry_node(state)
    assert result == {}


def test_prepare_retry_respects_the_cap(monkeypatch):
    monkeypatch.setattr(graph, "get_agent_config", lambda name: {"max_retries": 1})
    state = {
        "consistency_output": _consistency(conflicts=[_conflict(retry_target="policy_checker")]),
        "consistency_retry_counts": {"policy_checker": 1},  # already at the (lowered) cap
    }
    result = graph.prepare_consistency_retry_node(state)
    assert result == {}


def test_prepare_retry_no_op_when_no_conflicts():
    state = {"consistency_output": _consistency(has_conflicts=False), "consistency_retry_counts": {}}
    assert graph.prepare_consistency_retry_node(state) == {}
