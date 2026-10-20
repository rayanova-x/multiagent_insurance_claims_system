"""Hard-rule enforcement in src/agents/policy_checker.py.

policy_checker runs as a parallel fan-out branch (alongside damage_assessor
and fraud_crew - see src/agents/graph.py) and no longer computes
covered_amount_usd itself, since it has no access to the real damage figure
at the time it runs. Only one rule remains here:
  1. coverage_status is forced to NOT_COVERED when the claim type has no
     coverage at all (a fact already known before the LLM is called).
deductible_usd is still computed here (pure policy-record arithmetic, no
damage figure needed). covered_amount_usd's real arithmetic now lives in
graph.recompute_covered_amount_node, the post-fan-out join step - see
tests/test_graph_fanout.py for those tests.
"""

from types import SimpleNamespace

import src.agents.policy_checker as pc
from src.models.schemas import CoverageStatus, PolicyCheckOutput


def _coverage(covered, deductible=1000.0, limit=50000.0):
    return {"covered": covered, "deductible": deductible, "coverage_limit": limit}


def _output(status, covered_amount=0.0, deductible=0.0, exclusions=None):
    return PolicyCheckOutput(
        coverage_status=status, covered_amount_usd=covered_amount, deductible_usd=deductible,
        exclusions_triggered=exclusions or [], coverage_notes="model notes",
        compliance_flags=[], policy_limits={}, confidence=0.9,
    )


# ── Rule 1: not-covered type forces NOT_COVERED ─────────────────────────────

def test_llm_says_covered_but_type_has_no_coverage_forces_not_covered():
    output = _output(CoverageStatus.COVERED, covered_amount=10000.0)  # LLM disagreed with the known fact
    coverage = _coverage(covered=False)
    pc._enforce_hard_rules(output, coverage)
    assert output.coverage_status == CoverageStatus.NOT_COVERED


def test_covered_type_is_left_alone_by_rule_1():
    output = _output(CoverageStatus.COVERED, covered_amount=1000.0)
    coverage = _coverage(covered=True)
    pc._enforce_hard_rules(output, coverage)
    assert output.coverage_status == CoverageStatus.COVERED


# ── deductible_usd: pure arithmetic, no damage figure needed ───────────────

def test_deductible_usd_is_always_set_from_the_policy():
    output = _output(CoverageStatus.COVERED, deductible=0.0)  # LLM returned nothing useful
    coverage = _coverage(covered=True, deductible=2500.0)
    pc._enforce_hard_rules(output, coverage)
    assert output.deductible_usd == 2500.0


def test_enforce_hard_rules_does_not_touch_covered_amount():
    """covered_amount_usd is a placeholder here - _enforce_hard_rules must
    not compute or clamp it, since the real damage figure doesn't exist yet
    in this agent's own run. The post-fan-out join step owns that math."""
    output = _output(CoverageStatus.COVERED, covered_amount=12345.0)
    coverage = _coverage(covered=True, deductible=1000.0, limit=50000.0)
    pc._enforce_hard_rules(output, coverage)
    assert output.covered_amount_usd == 12345.0  # untouched


# ── Full agent: LLM-failure fallback also respects the remaining rule ──────

def test_llm_failure_fallback_does_not_crash_and_sets_deductible(monkeypatch):
    monkeypatch.setattr(pc, "lookup_policy", lambda num: {
        "policy_number": "POL-TEST", "type": "auto", "status": "active",
        "start_date": "2024-01-01", "end_date": "2026-01-01", "coverage": {"collision": 50000},
    })
    monkeypatch.setattr(pc, "get_coverage_for_claim_type", lambda policy, claim_type: _coverage(
        covered=True, deductible=1000.0, limit=50000.0,
    ))
    monkeypatch.setattr(pc, "mask_claim", lambda c: c)
    monkeypatch.setattr(pc, "log_agent_action", lambda **k: None)
    monkeypatch.setattr(
        pc, "get_structured_llm",
        lambda schema: SimpleNamespace(invoke=lambda msgs: (_ for _ in ()).throw(RuntimeError("provider down"))),
    )
    state = {
        "claim": {"claim_id": "CLM-PC1", "policy_number": "POL-TEST", "incident_type": "auto_collision",
                  "estimated_amount": 10000.0, "incident_date": "2025-01-01", "incident_description": "collision"},
        "masked_claim": {},
    }
    result = pc.run_policy_checker(state)
    output = result["policy_output"]
    assert output.coverage_status == CoverageStatus.NEEDS_VERIFICATION
    assert output.deductible_usd == 1000.0


def test_llm_failure_fallback_on_uncovered_type_forces_not_covered(monkeypatch):
    monkeypatch.setattr(pc, "lookup_policy", lambda num: {
        "policy_number": "POL-TEST", "type": "auto", "status": "active",
        "start_date": "2024-01-01", "end_date": "2026-01-01", "coverage": {},
    })
    monkeypatch.setattr(pc, "get_coverage_for_claim_type", lambda policy, claim_type: _coverage(covered=False))
    monkeypatch.setattr(pc, "mask_claim", lambda c: c)
    monkeypatch.setattr(pc, "log_agent_action", lambda **k: None)
    monkeypatch.setattr(
        pc, "get_structured_llm",
        lambda schema: SimpleNamespace(invoke=lambda msgs: (_ for _ in ()).throw(RuntimeError("provider down"))),
    )
    state = {
        "claim": {"claim_id": "CLM-PC2", "policy_number": "POL-TEST", "incident_type": "auto_theft",
                  "estimated_amount": 10000.0, "incident_date": "2025-01-01", "incident_description": "theft"},
        "masked_claim": {},
    }
    result = pc.run_policy_checker(state)
    output = result["policy_output"]
    assert output.coverage_status == CoverageStatus.NOT_COVERED
