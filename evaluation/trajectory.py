"""
Trajectory evaluation - was the PATH correct and efficient, not just the
final answer. Complements src/evaluation/evaluator.py, which grades the
completed settlement decision (outcome), and evaluation/validate_judges.py,
which grades the judges themselves. Neither checks whether a claim took a
sensible route through the graph to get there - a claim can land on the
right final decision through an unnecessarily expensive or wrong-looking
path, and outcome grading alone would call that a pass. See the baswe.ai
field guide's agent-evaluation chapter ("evaluate the journey, not just the
destination") - this project's earlier eval-infrastructure audit
(2026-10-19/20) only covered that guide's own "minimal credible stack"
checklist, which is outcome-only; this module fills the gap the later
"Evaluating AI agents" chapter actually names.

Three checks, all pure code against data this project already generates -
no LLM judge, no new quota spend, same "code first, judge only when code
can't verify" principle already governing the rest of this project
(src/evaluation/evaluator.py's own docstring):

  1. PATH CONFORMANCE: does the real pipeline_trace agent sequence match
     the branch's own documented expected path (data/test_scenarios/*.json's
     _scenario_note, hand-transcribed once into EXPECTED_PATHS below)?
     Catches "right final answer, wrong route" - the exact blind spot
     outcome-only evaluation has.
  2. STEP EFFICIENCY: actual agent_call_count vs. the expected path's own
     length. A claim that should resolve in 4 calls taking 7 is wandering
     even if it lands on a correct decision eventually.
  3. RETRY RECOVERY: for claims that went through consistency_coordinator's
     bounded retry loop (state["consistency_retry_counts"]), did a retry
     actually reduce the conflict count on the next pass, or did the claim
     exhaust the cap and escalate anyway? A code-computed recovered/
     escalated/not-applicable classification, from fields already written
     by src/agents/graph.py's own retry-routing logic.

Deliberately NOT built (see CLAUDE.md "do not add features beyond what the
task requires" and this project's own standing architecture note - code
decides routing, LLM never freely picks a tool or builds a tool call):
tool-selection accuracy and parameter accuracy do not apply to this system.
There is no LLM here that chooses among tools or constructs call arguments;
every agent calls a fixed, typed function, and every ROUTE between agents
is a deterministic code decision (route_after_intake, route_after_fan_out,
etc.), never an LLM's free choice. Grading "tool selection" would be
grading code that already can't be wrong in the way an agentic tool-calling
system can.

Usage:
    from evaluation.trajectory import score_trajectory
    result = score_trajectory(final_state, scenario_label="SC-06_normal_claim_fans_out")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Reference trajectories ──────────────────────────────────────────────────
# Hand-transcribed from each fixture's own `_scenario_note`
# (data/test_scenarios/*.json) - the branch's documented intent IS the
# reference path, not a separately invented "correct" answer. Entries use
# the real node names observed in live pipeline_trace output this session
# (see data/batch_results/2026-10-20_batch2_scenarios*.json). A path ending
# in "..." means "and then whatever communication_agent's own normal tail
# is" is not pinned exactly, because the branch's own note does not specify
# one (e.g. branches defined purely by WHERE they pause, not by what comes
# after a human resumes them with an unspecified decision).
#
# "min_calls"/"max_calls" are the expected agent_call_count RANGE for a claim
# that follows this path SPECIFICALLY under run_live_batch.py's INERT
# fraud_crew mock (agent_call_count=0 for that node - see
# scripts/run_live_batch.py's _mock_fraud_crew), the condition every live
# batch this session was actually run under. This is NOT the same as
# tests/_scenario_harness.py's ScenarioHarness default fraud mock, which
# costs 3 calls (matching fraud_crew's REAL cost: 1 outer call + 2 internal
# CrewAI steps) - confirmed by a real test failure while building this
# module (tests/test_trajectory.py's own history). Step-efficiency scoring
# against a ScenarioHarness-driven state, the real production API, or any
# future real (non-mocked) fraud_crew run will read as "wandering" against
# these ranges even when the path itself is correct - path_conformance
# (the AGENT SEQUENCE) stays valid across all of these; only the CALL-COUNT
# ranges are specific to the inert-mock condition. Recalibrate these ranges
# before relying on step_efficiency_ratio outside a run_live_batch.py-style
# inert-fraud-mock run.

@dataclass(frozen=True)
class ExpectedTrajectory:
    path: tuple[str, ...]
    min_calls: int
    max_calls: int
    note: str


EXPECTED_PATHS: dict[str, ExpectedTrajectory] = {
    "SC-01_invalid_claim_lapsed_policy": ExpectedTrajectory(
        ("intake_agent", "communication_agent"), 1, 1,
        "intake invalid -> straight to denial, no fan-out"),
    # NOTE on hitl_checkpoint: src/agents/graph.py only appends a
    # "hitl_checkpoint" pipeline_trace entry AFTER interrupt() returns with a
    # real resume decision - while still paused, nothing is appended for the
    # pause itself (confirmed against real saved batch output, not assumed -
    # see data/batch_results/2026-10-20_batch2_scenarios3.json's SC-03 entry:
    # pipeline_path is just ["intake_agent"] while paused=True). So a
    # still-PAUSED claim's expected path ends at whichever node triggered the
    # pause, not at "hitl_checkpoint" - that name only appears post-resume.
    "SC-02_prompt_injection_attempt": ExpectedTrajectory(
        ("intake_agent",), 1, 1,
        "injection flag at intake -> mandatory human review before fan-out (paused; path ends at intake_agent)"),
    "SC-03_disputed_incident_type": ExpectedTrajectory(
        ("intake_agent",), 1, 1,
        "disputed incident_type -> mandatory human review before fan-out (paused; path ends at intake_agent)"),
    "SC-05_fast_mode_small_claim": ExpectedTrajectory(
        ("intake_agent", "settlement_calculator", "evaluator", "communication_agent"), 2, 2,
        "below fast_mode.max_amount -> skips fan-out entirely"),
    "SC-06_normal_claim_fans_out": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker",
         "consistency_coordinator", "settlement_calculator", "evaluator", "communication_agent"),
        5, 6, "clean claim, full fan-out, no pauses"),
    "SC-07_guardrail_budget_halt": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker", "guardrails"),
        3, 3, "guardrail budget halt during fan-out, before consistency_coordinator "
              "(paused; confirmed live 2026-10-20 with MAX_AGENT_CALLS=2 override)"),
    # SC-08 deliberately excluded: its own branch definition requires
    # fraud_crew to return fraud_risk_level=CONFIRMED, which structurally
    # cannot happen while fraud_crew is mocked at an inert LOW/0.0 placeholder
    # (this project's standing scope decision for every live batch this
    # session - fraud_crew's own CrewAI internals were never redesigned).
    # Confirmed live (data/batch_results/2026-10-20_batch2_scenarios2.json):
    # SC-08 actually paused at consistency_coordinator via a DIFFERENT real
    # trigger (policy_checker's own compliance-flag check), never reaching
    # auto_reject. Giving this scenario a fixed expected path ending in
    # auto_reject would be grading against an outcome this test setup can
    # never produce - the same fabricated-ground-truth trap EXPECTED_PATHS'
    # own module docstring warns against.
    "SC-09_non_confirmed_fraud_proceeds": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker", "consistency_coordinator"),
        4, 6, "elevated but not CONFIRMED fraud -> proceeds like any other claim"),
    "SC-12_notable_conflict_no_retry": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker", "consistency_coordinator"),
        4, 6, "NOTABLE conflict surfaced but never triggers a retry"),
    "SC-14_physical_inspection_required": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker", "consistency_coordinator"),
        4, 4, "requires_physical_inspection -> always human review"),
    "SC-15_policy_exclusion_pre_existing_damage": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker", "consistency_coordinator"),
        4, 4, "policy exclusion -> always human review"),
    # SC-17/19/23 all depend on a companion *_documents.json fixture being
    # passed to process_claim(documents=...) - without it, damage_assessor's
    # requires_physical_inspection hard rule fires and the claim pauses at
    # consistency_coordinator instead (4 calls), confirmed as the real
    # observed path for all three the first time scenarios1/2/3 ran (before
    # the documents fixtures existed). The path below is the WITH-DOCUMENTS
    # expected path - each scenario's true intent per its own _scenario_note.
    "SC-17_clean_fanout_to_settlement": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker",
         "consistency_coordinator", "settlement_calculator", "evaluator", "communication_agent"),
        5, 6, "no conflicts/triggers/low confidence -> straight to settlement "
              "(requires documents - confirmed live 2026-10-20, settlement math exact: "
              "$4,500 - $2,025 depreciation - $500 deductible = $1,975)"),
    "SC-19_normal_settlement_to_evaluator": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker",
         "consistency_coordinator", "settlement_calculator", "evaluator", "communication_agent"),
        5, 6, "normal settlement confidence -> proceeds to evaluator as usual "
              "(requires documents - NOT yet live-confirmed with documents attached; "
              "the one run that used this fixture's _documents.json ran it as a "
              "standalone bad entry from the scenario-glob bug, not a valid test)"),
    "SC-23_passed_low_value_to_communication": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker",
         "consistency_coordinator", "settlement_calculator", "evaluator", "communication_agent"),
        5, 6, "simplest full-pipeline completion "
              "(requires documents - confirmed live 2026-10-20 in the corrected "
              "scenarios3 run, final_amount_usd=$160)"),
    "SC-30_low_confidence_low_value": ExpectedTrajectory(
        ("intake_agent", "damage_assessor", "fraud_crew", "policy_checker", "consistency_coordinator"),
        4, 6, "low-value ambiguous narrative - confidence alone must NOT pause here"),
    # Branches whose own note says the result depends on a real (non-mocked)
    # judgment call that cannot be pinned to one exact expected path
    # (SC-10/11/13/16/18/20/21/22/24/25/26/27/28/29/04) are deliberately left
    # out of this table rather than given a guessed entry - see
    # "What this does NOT claim to cover" below.
}


# ── Scoring ──────────────────────────────────────────────────────────────

@dataclass
class TrajectoryResult:
    scenario_label: Optional[str]
    actual_path: list[str]
    actual_calls: int
    expected: Optional[ExpectedTrajectory]
    path_conforms: Optional[bool]       # None when no reference exists
    step_efficiency_ratio: Optional[float]  # actual_calls / expected.max_calls
    retry_recovery: str                  # "recovered" | "escalated" | "not_applicable"
    notes: list[str] = field(default_factory=list)


def _retry_recovery_status(state: dict) -> str:
    """Code-computed, from fields src/agents/graph.py already writes.

    A claim only has a retry history at all if consistency_coordinator
    actually requested one (state["consistency_retry_counts"] non-empty).
    Reads state["consistency_output"].has_conflicts directly (the real,
    final ConsistencyCheckOutput object for whichever pass ran last), NOT
    pipeline_trace's own "decision" string - the real agent
    (src/agents/consistency_coordinator.py) writes a matching "consistent"/
    "conflicts_found" string into its trace entry, but tests/
    _scenario_harness.py's mocked ScenarioHarness does not replicate that
    field (confirmed by a real test failure while building this module -
    see tests/test_trajectory.py's own history), so reading the trace
    string would silently break under the test harness even though it's
    correct against a live run. state["consistency_output"] is populated
    identically by both, since both set it directly as their node's own
    primary state update - a more robust source of truth than re-deriving
    the same fact from a second, log-shaped copy of it.
    "Recovered" means has_conflicts is False after retries ran. "Escalated"
    means the retry cap was used up and has_conflicts is still True.
    """
    retry_counts = state.get("consistency_retry_counts") or {}
    if not retry_counts:
        return "not_applicable"

    consistency_output = state.get("consistency_output")
    if consistency_output is None:
        return "not_applicable"

    has_conflicts = getattr(consistency_output, "has_conflicts", None)
    if has_conflicts is None:
        return "not_applicable"
    return "escalated" if has_conflicts else "recovered"


def score_trajectory(state: dict, scenario_label: Optional[str] = None) -> TrajectoryResult:
    """Score one completed (or paused) claim's real trajectory.

    state: the ClaimsState dict returned as result["state"] from
    process_claim()/resume_claim() (src/agents/graph.py).
    scenario_label: a data/test_scenarios/*.json stem, to look up its
    reference path in EXPECTED_PATHS. None (or an unmapped label) means
    "no reference available" - path_conforms and step_efficiency_ratio
    come back None rather than a guessed verdict; this is a legitimate
    outcome, not an error (see src/evaluation/evaluator.py's own precedent
    for an honest "I don't know" rather than a forced score).
    """
    actual_path = [
        e.get("agent") for e in (state.get("pipeline_trace") or [])
        if isinstance(e, dict) and e.get("agent")
    ]
    actual_calls = int(state.get("agent_call_count") or 0)
    notes: list[str] = []

    expected = EXPECTED_PATHS.get(scenario_label) if scenario_label else None
    path_conforms: Optional[bool] = None
    step_efficiency_ratio: Optional[float] = None

    if expected is not None:
        path_conforms = tuple(actual_path) == expected.path
        if not path_conforms:
            notes.append(
                f"path diverges from reference: expected {list(expected.path)}, got {actual_path}"
            )
        if expected.max_calls > 0:
            step_efficiency_ratio = round(actual_calls / expected.max_calls, 2)
            if actual_calls > expected.max_calls:
                notes.append(
                    f"used {actual_calls} real agent calls, expected at most {expected.max_calls} "
                    f"for this branch ({expected.note})"
                )
    else:
        notes.append(
            "no reference trajectory for this scenario_label - either not passed, or this "
            "branch's own outcome genuinely depends on a real judgment call that can't be "
            "pinned to one fixed expected path (see EXPECTED_PATHS' own exclusion note)"
        )

    retry_recovery = _retry_recovery_status(state)
    if retry_recovery == "escalated":
        notes.append("consistency_coordinator retry cap exhausted without resolving the conflict")

    return TrajectoryResult(
        scenario_label=scenario_label,
        actual_path=actual_path,
        actual_calls=actual_calls,
        expected=expected,
        path_conforms=path_conforms,
        step_efficiency_ratio=step_efficiency_ratio,
        retry_recovery=retry_recovery,
        notes=notes,
    )


def score_trajectory_from_summary(summary: dict, scenario_label: Optional[str] = None) -> TrajectoryResult:
    """Score a claim from the LIGHTER saved shape data/batch_results/*.json
    actually uses (a flat "pipeline_path": [agent_name, ...] list plus
    "agent_call_count" - NOT the full ClaimsState dict score_trajectory()
    expects, since those batch summaries were written before this module
    existed and never saved full state, only this leaner per-claim record).

    Path-conformance and step-efficiency both work identically from this
    shape. Retry-recovery does NOT: that needs state["consistency_output"]
    and state["consistency_retry_counts"], neither of which this saved
    format carries, so retry_recovery always comes back "not_applicable"
    here even for a claim that really did retry - a real, stated limitation
    of scoring old saved summaries rather than a fresh live state dict, not
    a silent wrong answer.
    """
    fake_state = {
        "pipeline_trace": [{"agent": a} for a in (summary.get("pipeline_path") or [])],
        "agent_call_count": summary.get("agent_call_count", 0),
    }
    result = score_trajectory(fake_state, scenario_label=scenario_label)
    result.notes.append(
        "retry_recovery is not determinable from a saved batch-result summary (no "
        "consistency_output/consistency_retry_counts in this shape) - always "
        "'not_applicable' here regardless of what really happened live"
    )
    return result


# ── What this does NOT claim to cover ───────────────────────────────────────
# 1. Branches whose real path depends on live LLM judgment (confidence gates,
#    fraud-score thresholds, evaluator quality-gate outcomes, resume-decision
#    branches) are intentionally absent from EXPECTED_PATHS - a single fixed
#    "expected path" would be a fabricated ground truth for something that
#    legitimately varies, the same trap this project's damage-cost lookup
#    table was retired for (src/tools/damage_calculator.py's own docstring).
#    Running these scenarios still produces a TrajectoryResult; it just
#    reports path_conforms=None/step_efficiency_ratio=None honestly instead
#    of grading against an invented reference.
# 2. Tool-selection and parameter-accuracy metrics (the field guide's other
#    two trajectory metrics) are not implemented at all - see this module's
#    own docstring for why they do not apply to a code-routed architecture.
# 3. This is a one-off analysis module, not wired into evaluator.py's live
#    per-claim scoring or into any CI gate - it is meant to be run against
#    saved batch results (data/batch_results/*.json) or directly against a
#    fresh process_claim() result, the same way evaluation/run_eval.py is
#    run manually today. Wiring it into the live graph or CI was not asked
#    for and would be scope creep beyond what this round's request covers.
