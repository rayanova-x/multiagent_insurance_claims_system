"""Unit tests for evaluation/validate_judges.py's grading functions -
_grade_consistency and _grade_score. These are pure functions (no LLM
calls), so they run in the normal CI gate; the harness's actual LIVE
validation run (real Gemini calls against evaluation/judge_golden_set.py)
is a separate, manually-triggered step - see .github/workflows/llm-eval.yml.
"""

from evaluation.validate_judges import _grade_consistency, _grade_score


def test_grade_consistency_agrees_when_both_say_no_conflict():
    assert _grade_consistency(
        {"has_conflicts": False, "conflict_type": None},
        {"has_conflicts": False, "conflict_type": None},
    ) is True


def test_grade_consistency_disagrees_on_has_conflicts_mismatch():
    assert _grade_consistency(
        {"has_conflicts": False, "conflict_type": None},
        {"has_conflicts": True, "conflict_type": "missed_fact"},
    ) is False


def test_grade_consistency_agrees_on_matching_conflict_type():
    assert _grade_consistency(
        {"has_conflicts": True, "conflict_type": "severity_mismatch"},
        {"has_conflicts": True, "conflict_type": "severity_mismatch"},
    ) is True


def test_grade_consistency_disagrees_on_wrong_conflict_type():
    """Both agree a conflict exists, but the judge named the wrong kind -
    still a real disagreement, not a partial credit case."""
    assert _grade_consistency(
        {"has_conflicts": True, "conflict_type": "missed_fact"},
        {"has_conflicts": True, "conflict_type": "severity_mismatch"},
    ) is False


def test_grade_score_within_tolerance_agrees():
    assert _grade_score(0.85, 1.0, tolerance=0.3) is True


def test_grade_score_outside_tolerance_disagrees():
    assert _grade_score(0.5, 1.0, tolerance=0.3) is False


def test_grade_score_at_exact_tolerance_boundary_agrees():
    """0.7 vs 1.0 at tolerance=0.3 lands exactly on the float boundary
    (abs(0.7 - 1.0) == 0.29999999999999993 due to binary float
    representation, not 0.3) - uses a deliberately non-boundary pair
    instead so the test checks the intended behavior, not float rounding."""
    assert _grade_score(0.71, 1.0, tolerance=0.3) is True


def test_grade_score_none_actual_always_disagrees():
    """A judge that couldn't decide (score=None) is graded as wrong against
    any non-null expected label - a real miscalibration signal, not a
    free pass."""
    assert _grade_score(None, 1.0) is False
