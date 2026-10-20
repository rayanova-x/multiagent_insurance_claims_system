"""guard_node: the wrapper every agent node runs through.

Before this, GuardrailsManager existed only in its own module and this test
suite: budgets in base.yaml were never enforced in the pipeline, and
guardrails_violations was always empty.
"""

import threading

from src.guardrails.manager import guard_node
from src.llm import get_token_usage, record_external_usage, reset_token_tracking


def _state(**extra):
    base = {
        "claim": {"claim_id": "CLM-G-1"},
        "agent_call_count": 0,
        "total_tokens_used": 0,
        "total_cost_usd": 0.0,
        "processing_seconds": 0.0,
        "guardrails_violations": [],
        "pipeline_trace": [],
    }
    base.update(extra)
    return base


def test_over_budget_claim_is_halted_and_the_agent_never_runs():
    called = []
    node = guard_node("damage_assessor", lambda s: called.append(1) or {})
    update = node(_state(total_cost_usd=0.75))   # max_cost_usd is 0.50
    assert called == []
    assert update["guardrails_halted"] is True
    assert update["final_decision"].value == "escalated_human_review"
    assert any("Cost budget exceeded" in v for v in update["guardrails_violations"])
    assert update["pipeline_trace"][0]["skipped_agent"] == "damage_assessor"


def test_timeout_counts_active_processing_time_not_time_paused_for_a_reviewer():
    node = guard_node("policy_checker", lambda s: {})
    assert node(_state(processing_seconds=601)).get("guardrails_halted") is True   # max_execution_seconds 600
    # A claim that sat in the review queue for hours but only ran briefly is fine.
    assert "guardrails_halted" not in node(_state(processing_seconds=12, execution_start_time="2020-01-01"))


def test_usage_is_a_per_node_delta_not_a_running_total():
    """guard_node now returns only what THIS node itself spent, not a
    running total read from state and added to - total_tokens_used/
    total_cost_usd/processing_seconds are Annotated[..., operator.add] in
    ClaimsState (src/models/state.py) precisely so LangGraph's own reducer
    does the summing. This matters because damage_assessor, policy_checker,
    and fraud_crew now run as parallel fan-out branches (src/agents/graph.py)
    and guard_node wraps all three: if guard_node itself tried to compute a
    running total (old behavior), two concurrent branches reading the SAME
    starting total and each adding their own delta would under-count by
    double-ignoring each other's contribution once the reducer also sums
    them - the delta-only approach is correct under concurrency, a
    running-total return is not."""
    reset_token_tracking()

    def agent(state):
        record_external_usage(1000, 200)
        return {"intake_output": None}

    # total_tokens_used=500 in starting state must be IGNORED by guard_node -
    # that's the reducer's job when this update is actually merged by
    # LangGraph, not guard_node's.
    update = guard_node("intake_agent", agent)(_state(total_tokens_used=500))
    assert update["total_tokens_used"] == 1200  # 1000 + 200, this node's own spend only
    assert update["processing_seconds"] >= 0


def test_communication_always_runs_even_over_budget():
    node = guard_node("communication_agent", lambda s: {"ran": True}, enforce_budget=False)
    assert node(_state(total_cost_usd=9.99))["ran"] is True


def test_token_usage_is_isolated_per_thread():
    # Each claim runs in its own thread. A module-level counter let two
    # concurrent claims add to (and reset) the same totals.
    results = {}
    barrier = threading.Barrier(2)

    def run(name, tokens):
        reset_token_tracking()
        barrier.wait()
        record_external_usage(tokens, 0)
        barrier.wait()
        results[name] = get_token_usage()["total"]

    threads = [threading.Thread(target=run, args=("a", 111)), threading.Thread(target=run, args=("b", 999))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"a": 111, "b": 999}
