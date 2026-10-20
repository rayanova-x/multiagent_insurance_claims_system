"""src/evaluation/evaluator.py: accuracy is code-verified (not an LLM call),
fairness/transparency are isolated judge calls, and an unresolved dimension
(score=None) must prevent auto-release even if everything else looks fine.
"""

from types import SimpleNamespace

import src.evaluation.evaluator as ev
from src.models.schemas import ClaimDecision, CoverageStatus, JudgeDimensionOutput


def _settlement(amount, gross, deductible):
    return SimpleNamespace(
        decision=ClaimDecision.APPROVED, settlement_amount_usd=amount,
        gross_damage_usd=gross, deductible_applied_usd=deductible, depreciation_applied_usd=0.0,
        calculation_breakdown=["step"], denial_reasons=[],
    )


def _policy(covered_amount=100_000.0):
    return SimpleNamespace(coverage_status=CoverageStatus.COVERED, covered_amount_usd=covered_amount,
                           exclusions_triggered=[])


# ── _verify_accuracy: pure code, no LLM involved ────────────────────────────

def test_accuracy_verified_when_amount_respects_both_caps():
    settlement = _settlement(amount=9000.0, gross=10000.0, deductible=1000.0)
    ok, detail = ev._verify_accuracy(settlement, _policy(covered_amount=50_000.0))
    assert ok is True


def test_accuracy_fails_when_amount_exceeds_damage_minus_deductible():
    settlement = _settlement(amount=15000.0, gross=10000.0, deductible=1000.0)  # should be capped at 9000
    ok, detail = ev._verify_accuracy(settlement, _policy(covered_amount=50_000.0))
    assert ok is False
    assert "exceeds damage minus deductible" in detail


def test_accuracy_fails_when_amount_exceeds_coverage_limit():
    settlement = _settlement(amount=9000.0, gross=10000.0, deductible=1000.0)
    ok, detail = ev._verify_accuracy(settlement, _policy(covered_amount=5000.0))  # lower than 9000
    assert ok is False
    assert "exceeds the policy's covered amount" in detail


def test_accuracy_fails_when_no_settlement_exists():
    ok, detail = ev._verify_accuracy(None, _policy())
    assert ok is False


def test_accuracy_check_never_calls_an_llm(monkeypatch):
    """The whole point of this redesign: accuracy is code, not a judge call."""
    monkeypatch.setattr(ev, "get_judge_llm", lambda: (_ for _ in ()).throw(
        AssertionError("accuracy must never call the judge LLM")))
    settlement = _settlement(amount=9000.0, gross=10000.0, deductible=1000.0)
    ev._verify_accuracy(settlement, _policy())  # must not raise


# ── overall_score / passed: computed by code from the three components ────

def _run_with_mocked_judges(monkeypatch, fairness_score, transparency_score, accuracy_ok=True):
    monkeypatch.setattr(ev, "get_evaluation_config", lambda: {"batch_eval_sample_rate": 1.0, "min_score_to_release": 0.70})
    monkeypatch.setattr("src.config.get_hitl_config", lambda: {"triggers": {"min_amount": 999_999_999}})
    monkeypatch.setattr(ev, "mask_claim", lambda c: c)
    monkeypatch.setattr(ev, "log_agent_action", lambda **k: None)

    settlement = _settlement(amount=9000.0, gross=10000.0, deductible=1000.0) if accuracy_ok \
        else _settlement(amount=99999.0, gross=10000.0, deductible=1000.0)

    calls = iter([
        JudgeDimensionOutput(score=fairness_score, reasoning="fairness reasoning"),
        JudgeDimensionOutput(score=transparency_score, reasoning="transparency reasoning"),
    ])
    monkeypatch.setattr(ev, "_judge_dimension", lambda *a, **k: next(calls))

    state = {
        "claim": {"claim_id": "CLM-EV1", "estimated_amount": 1000, "incident_type": "auto_collision",
                  "incident_description": "test"},
        "masked_claim": {"incident_description": "test"},
        "settlement_output": settlement, "policy_output": _policy(), "damage_output": None,
        "fraud_output": None, "hitl_required": False, "human_override": False,
    }
    return ev.run_evaluator(state)


def test_both_dimensions_good_and_accuracy_ok_passes(monkeypatch):
    result = _run_with_mocked_judges(monkeypatch, fairness_score=0.9, transparency_score=0.9)
    output = result["evaluation_output"]
    assert output.accuracy_verified is True
    assert output.passed is True
    assert result["evaluation_passed"] is True


def test_accuracy_failure_fails_the_whole_evaluation_even_with_good_judge_scores(monkeypatch):
    result = _run_with_mocked_judges(monkeypatch, fairness_score=0.95, transparency_score=0.95, accuracy_ok=False)
    output = result["evaluation_output"]
    assert output.accuracy_verified is False
    assert output.passed is False
    assert any("accuracy_check_failed" in f for f in output.flags)


def test_unresolved_fairness_dimension_prevents_passing_even_if_transparency_is_perfect(monkeypatch):
    """score=None means 'the judge couldn't tell', not 'everything is fine' -
    this must never be silently treated as a pass."""
    result = _run_with_mocked_judges(monkeypatch, fairness_score=None, transparency_score=1.0)
    output = result["evaluation_output"]
    assert output.passed is False
    assert any("fairness_unresolved" in f for f in output.flags)


def test_low_fairness_score_fails_the_quality_gate(monkeypatch):
    # overall_score = mean(accuracy=1.0, fairness=0.1, transparency=0.9) = 0.667 < 0.70
    result = _run_with_mocked_judges(monkeypatch, fairness_score=0.1, transparency_score=0.9)
    output = result["evaluation_output"]
    assert output.overall_score < 0.70
    assert output.passed is False


# ── Isolated calls: fairness and transparency never share one LLM call ─────

def test_fairness_and_transparency_are_separate_isolated_calls(monkeypatch):
    """Per Anthropic's guidance: grade each dimension with an isolated judge
    call, not one call scoring several dimensions. Assert _judge_dimension
    is invoked twice, once per dimension, each with its own system prompt."""
    monkeypatch.setattr(ev, "get_evaluation_config", lambda: {"batch_eval_sample_rate": 1.0, "min_score_to_release": 0.70})
    monkeypatch.setattr("src.config.get_hitl_config", lambda: {"triggers": {"min_amount": 999_999_999}})
    monkeypatch.setattr(ev, "mask_claim", lambda c: c)
    monkeypatch.setattr(ev, "log_agent_action", lambda **k: None)

    prompts_seen = []
    def fake_judge(system_prompt, user_prompt):
        prompts_seen.append(system_prompt)
        return JudgeDimensionOutput(score=0.8, reasoning="ok")
    monkeypatch.setattr(ev, "_judge_dimension", fake_judge)

    settlement = _settlement(amount=9000.0, gross=10000.0, deductible=1000.0)
    state = {
        "claim": {"claim_id": "CLM-EV2", "estimated_amount": 1000, "incident_type": "auto_collision",
                  "incident_description": "test"},
        "masked_claim": {"incident_description": "test"},
        "settlement_output": settlement, "policy_output": _policy(), "damage_output": None,
        "fraud_output": None, "hitl_required": False, "human_override": False,
    }
    ev.run_evaluator(state)

    assert len(prompts_seen) == 2
    assert prompts_seen[0] != prompts_seen[1]  # genuinely different, isolated prompts
    assert prompts_seen[0] == ev.FAIRNESS_SYSTEM_PROMPT
    assert prompts_seen[1] == ev.TRANSPARENCY_SYSTEM_PROMPT


# ── agent_call_count: the original evaluator never incremented it ──────────

def test_agent_call_count_is_incremented(monkeypatch):
    result = _run_with_mocked_judges(monkeypatch, fairness_score=0.9, transparency_score=0.9)
    assert result["agent_call_count"] == 1
