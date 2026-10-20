"""The fan-out join step: recompute_covered_amount_node.

policy_checker runs as a parallel fan-out branch and no longer computes
covered_amount_usd itself (it has no access to the real damage figure at
the time it runs - see src/agents/policy_checker.py's module docstring).
This is the code-only join step that recomputes it once damage_assessor's
real output is available, mirroring the exact formula policy_checker used
to apply internally: min(damage - deductible, limit - deductible), floored
at 0, with a NOT_COVERED claim always paying 0.
"""

from types import SimpleNamespace

from src.agents import graph
from src.models.schemas import CoverageStatus


def _policy_output(status=CoverageStatus.COVERED, deductible=1000.0, covered_amount=0.0):
    return SimpleNamespace(coverage_status=status, deductible_usd=deductible, covered_amount_usd=covered_amount)


def _damage_output(assessed=10000.0):
    return SimpleNamespace(assessed_damage_usd=assessed)


def test_covered_amount_is_recomputed_from_the_real_damage_figure(monkeypatch):
    monkeypatch.setattr(graph, "lookup_policy", lambda num: {"policy_number": num})
    monkeypatch.setattr(graph, "get_coverage_for_claim_type", lambda policy, claim_type: {
        "coverage_limit": 50000.0,
    })
    state = {
        "policy_output": _policy_output(deductible=1000.0),
        "damage_output": _damage_output(assessed=10000.0),
        "claim": {"policy_number": "POL-1", "incident_type": "auto_collision", "estimated_amount": 10000.0},
    }
    result = graph.recompute_covered_amount_node(state)
    # min(10000 - 1000, 50000 - 1000) = 9000
    assert result["policy_output"].covered_amount_usd == 9000.0


def test_covered_amount_capped_by_coverage_limit(monkeypatch):
    monkeypatch.setattr(graph, "lookup_policy", lambda num: {"policy_number": num})
    monkeypatch.setattr(graph, "get_coverage_for_claim_type", lambda policy, claim_type: {
        "coverage_limit": 20000.0,
    })
    state = {
        "policy_output": _policy_output(deductible=1000.0),
        "damage_output": _damage_output(assessed=100_000.0),
        "claim": {"policy_number": "POL-1", "incident_type": "auto_collision", "estimated_amount": 100_000.0},
    }
    result = graph.recompute_covered_amount_node(state)
    # min(100000-1000, 20000-1000) = 19000
    assert result["policy_output"].covered_amount_usd == 19000.0


def test_covered_amount_floors_at_zero(monkeypatch):
    monkeypatch.setattr(graph, "lookup_policy", lambda num: {"policy_number": num})
    monkeypatch.setattr(graph, "get_coverage_for_claim_type", lambda policy, claim_type: {
        "coverage_limit": 50000.0,
    })
    state = {
        "policy_output": _policy_output(deductible=5000.0),
        "damage_output": _damage_output(assessed=1000.0),  # damage < deductible
        "claim": {"policy_number": "POL-1", "incident_type": "auto_collision", "estimated_amount": 1000.0},
    }
    result = graph.recompute_covered_amount_node(state)
    assert result["policy_output"].covered_amount_usd == 0.0


def test_not_covered_status_always_pays_zero_regardless_of_damage(monkeypatch):
    monkeypatch.setattr(graph, "lookup_policy", lambda num: {"policy_number": num})
    monkeypatch.setattr(graph, "get_coverage_for_claim_type", lambda policy, claim_type: {
        "coverage_limit": 50000.0,
    })
    state = {
        "policy_output": _policy_output(status=CoverageStatus.NOT_COVERED, deductible=1000.0),
        "damage_output": _damage_output(assessed=10000.0),
        "claim": {"policy_number": "POL-1", "incident_type": "auto_collision", "estimated_amount": 10000.0},
    }
    result = graph.recompute_covered_amount_node(state)
    assert result["policy_output"].covered_amount_usd == 0.0


def test_falls_back_to_claimant_estimate_when_damage_output_is_missing(monkeypatch):
    """Defensive fallback - in the real pipeline damage_output always exists
    by the time this join step runs (all three fan-out branches complete
    before it), but the fallback mirrors the same pattern used elsewhere
    (e.g. settlement_calculator's fast-mode path) rather than crashing."""
    monkeypatch.setattr(graph, "lookup_policy", lambda num: {"policy_number": num})
    monkeypatch.setattr(graph, "get_coverage_for_claim_type", lambda policy, claim_type: {
        "coverage_limit": 50000.0,
    })
    state = {
        "policy_output": _policy_output(deductible=1000.0),
        "damage_output": None,
        "claim": {"policy_number": "POL-1", "incident_type": "auto_collision", "estimated_amount": 10000.0},
    }
    result = graph.recompute_covered_amount_node(state)
    assert result["policy_output"].covered_amount_usd == 9000.0


def test_no_op_when_policy_output_is_missing():
    """Defensive - should never happen in the real pipeline (policy_checker
    always runs as part of the fan-out), but must not crash the join step."""
    result = graph.recompute_covered_amount_node({"damage_output": _damage_output(), "claim": {}})
    assert result == {}
