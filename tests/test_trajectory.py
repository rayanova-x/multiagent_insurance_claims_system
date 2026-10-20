"""Structural tests for evaluation/trajectory.py, using the same mocked
ScenarioHarness as tests/test_scenarios_e2e.py - no LLM calls, no quota
spent. These confirm the scoring LOGIC is sound (path-conformance diffing,
step-efficiency math, retry-recovery classification); the EXPECTED_PATHS
table's own entries were separately cross-checked against this session's
real saved batch results (data/batch_results/2026-10-20_batch2_scenarios*.
json) rather than invented, see the table's own inline comments.
"""

from __future__ import annotations

from evaluation.trajectory import EXPECTED_PATHS, score_trajectory
from src.models.schemas import ConflictSeverity, ConflictType
from tests._scenario_harness import (
    ScenarioHarness,
    make_claim,
    make_conflict,
    make_consistency,
    run_scenario,
)


def test_path_conforms_for_a_known_clean_scenario():
    """SC-06's reference path is a clean, no-document-dependent fan-out that
    this harness's own defaults should reproduce exactly (agent SEQUENCE,
    not call count - ScenarioHarness's default fraud mock costs 3 calls,
    matching fraud_crew's real cost, unlike EXPECTED_PATHS' max_calls
    figures, which assume run_live_batch.py's inert 0-cost mock - see this
    test file's own module docstring and test_step_efficiency_flags_an_
    overlong_path, which exercises the call-count math directly instead)."""
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-TRAJ-06"))
    scored = score_trajectory(result["state"], scenario_label="SC-06_normal_claim_fans_out")
    assert scored.expected is not None
    assert scored.path_conforms is True


def test_path_conformance_catches_a_real_divergence():
    """A deliberately wrong scenario_label (mapped to a path this run will
    never take) must come back path_conforms=False with an explanatory note
    - this is the whole point of the module: catching 'right answer, wrong
    route' that outcome-only grading misses."""
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-TRAJ-06B"))
    scored = score_trajectory(result["state"], scenario_label="SC-01_invalid_claim_lapsed_policy")
    assert scored.path_conforms is False
    assert any("diverges from reference" in n for n in scored.notes)


def test_no_reference_scenario_label_is_honest_not_a_guess():
    """An unmapped or missing scenario_label must come back with
    path_conforms=None / step_efficiency_ratio=None, never a fabricated
    verdict - same 'I don't know' precedent as src/evaluation/evaluator.py's
    own JudgeDimensionOutput.score: Optional[float]."""
    h = ScenarioHarness()
    result = run_scenario(h, make_claim("CLM-TRAJ-UNMAPPED"))
    scored = score_trajectory(result["state"], scenario_label=None)
    assert scored.expected is None
    assert scored.path_conforms is None
    assert scored.step_efficiency_ratio is None
    assert scored.notes

    scored_unmapped = score_trajectory(result["state"], scenario_label="SC-99_not_a_real_scenario")
    assert scored_unmapped.expected is None
    assert scored_unmapped.path_conforms is None


def test_retry_recovery_recovered():
    """Branch 10 (tests/test_scenarios_e2e.py's own test_10): a MATERIAL
    conflict resolves after one retry -> must classify as 'recovered'."""
    conflict = make_conflict(conflict_type=ConflictType.SEVERITY_MISMATCH,
                              severity=ConflictSeverity.MATERIAL, retry_target="damage_assessor")
    h = ScenarioHarness(consistency_sequence=[
        make_consistency(has_conflicts=True, conflicts=[conflict]),
        make_consistency(has_conflicts=False),
    ])
    result = run_scenario(h, make_claim("CLM-TRAJ-10"))
    scored = score_trajectory(result["state"])
    assert scored.retry_recovery == "recovered"
    assert not any("retry cap exhausted" in n for n in scored.notes)


def test_retry_recovery_escalated():
    """Branch 11 (tests/test_scenarios_e2e.py's own test_11): a MATERIAL
    conflict never resolves, retry cap exhausted -> must classify as
    'escalated', with an explanatory note."""
    conflict = make_conflict(conflict_type=ConflictType.MISSED_FACT,
                              severity=ConflictSeverity.MATERIAL, retry_target="policy_checker")
    h = ScenarioHarness(consistency_sequence=[make_consistency(has_conflicts=True, conflicts=[conflict])])
    result = run_scenario(h, make_claim("CLM-TRAJ-11"))
    scored = score_trajectory(result["state"])
    assert scored.retry_recovery == "escalated"
    assert any("retry cap exhausted" in n for n in scored.notes)


def test_retry_recovery_not_applicable_when_no_retry_happened():
    """Branch 12: a NOTABLE conflict never triggers a retry at all ->
    retry_recovery must be 'not_applicable', not a false 'recovered'."""
    conflict = make_conflict(severity=ConflictSeverity.NOTABLE, retry_target="damage_assessor")
    h = ScenarioHarness(consistency=make_consistency(has_conflicts=True, conflicts=[conflict]))
    result = run_scenario(h, make_claim("CLM-TRAJ-12"))
    scored = score_trajectory(result["state"])
    assert scored.retry_recovery == "not_applicable"


def test_step_efficiency_flags_an_overlong_path():
    """A claim that burns more real agent calls than its branch's own
    max_calls should flag it, even if path_conforms matches on agent
    sequence alone (e.g. a retry loop re-visits the same named agents,
    so the ORDERED sequence can still differ in length only)."""
    scored = score_trajectory(
        {"pipeline_trace": [
            {"agent": a} for a in
            ["intake_agent", "damage_assessor", "fraud_crew", "policy_checker", "consistency_coordinator"]
        ], "agent_call_count": 10},
        scenario_label="SC-14_physical_inspection_required",
    )
    assert scored.expected is not None
    assert scored.step_efficiency_ratio is not None
    assert scored.step_efficiency_ratio > 1.0
    assert any("used 10 real agent calls" in n for n in scored.notes)


def test_expected_paths_table_has_no_excluded_scenarios():
    """SC-08 is deliberately excluded (structurally unreachable while
    fraud_crew is mocked) - guard against it silently creeping back in."""
    assert "SC-08_confirmed_fraud" not in EXPECTED_PATHS
