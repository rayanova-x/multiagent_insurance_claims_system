"""Runs a real live batch (real Gemini calls, real pipeline, real
checkpointer) with fraud_crew's CrewAI internals mocked out - per explicit
user scope decision: fraud_crew was never redesigned this session and its
own LLM calls should not run today, while everything else (intake, damage,
policy, consistency_coordinator, settlement, evaluator, communication)
should call the real model.

fraud_crew's mock reuses the project's own EXISTING "couldn't assess, ask a
human" shape from its own real error-handling fallback (see
src/agents/fraud_crew.py's except block) - not a fabricated LOW/HIGH risk
judgment, which would itself bias what consistency_coordinator and the
routers see. This is an honest "no opinion formed" placeholder, the same
shape the real code already falls back to when fraud_crew genuinely cannot
run (e.g. mid-run quota exhaustion, confirmed live earlier this session).

IMPORTANT (confirmed empirically this session, see CLAUDE.md): the compiled
LangGraph graph binds node functions to their agent functions BY VALUE at
build time (_build_graph_object + get_compiled_graph's module-level cache).
patch.object(graph, "run_fraud_crew", fake) therefore only takes effect if
applied BEFORE the graph is first compiled in this process - this script
patches first, then lets process_claim build (and cache) the graph under
that patch, exactly like tests/_scenario_harness.py does for the fully-
mocked test suite.

Usage:
    python scripts/run_live_batch.py golden        # judge golden set only
    python scripts/run_live_batch.py sample         # the 5 US auto_collision sample_claims
    python scripts/run_live_batch.py both           # golden + sample (today's real batch)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agents import graph as g
from src.models.schemas import FraudAssessmentOutput, FraudRiskLevel
from src.models.state import ClaimInput

# ── fraud_crew mock: inert placeholder, deliberately NOT the real error-
# fallback shape. fraud_crew's own real except-block fallback (MEDIUM risk,
# score 0.50) is correct for a genuine failure - it should escalate to
# human review, since something real actually went wrong. Today's case is
# different: fraud_crew is being skipped on PURPOSE (out of scope, never
# redesigned this session), not failing - using the real failure shape
# would mean fraud_score=0.50 sits right at hitl.triggers.fraud_score's
# 0.45 review threshold and every single claim in the batch would pause
# for HITL regardless of what the rest of the pipeline concludes, which
# would hide whether damage/policy/consistency/settlement/evaluator reach
# a correct decision on their own. So this uses an inert LOW/0.0 placeholder
# instead - explicit user decision, confirmed before running any real
# quota against it.

def _mock_fraud_crew(state: dict) -> dict:
    output = FraudAssessmentOutput(
        fraud_risk_level=FraudRiskLevel.LOW,
        fraud_score=0.0,
        primary_concerns=[],
        recommendation="proceed",
        crew_summary="fraud_crew NOT ASSESSED - intentionally excluded from this batch run (out of scope, "
                      "its CrewAI internals were never redesigned this session). This is an inert placeholder, "
                      "not a real fraud judgment - do not treat fraud_score=0.0 as evidence this claim is low risk.",
        pattern_score=0.0,
        anomaly_score=0.0,
        consistency_score=1.0,
        narrative_risk=None,
        confidence=0.0,
    )
    return {
        "fraud_output": output,
        "pipeline_trace": [{
            "agent": "fraud_crew",
            "framework": "crewai",
            "fraud_score": output.fraud_score,
            "risk_level": output.fraud_risk_level.value,
            "duration_ms": 0,
            "confidence": output.confidence,
            "decision": output.recommendation,
            "reasoning": output.crew_summary,
            "flags": output.primary_concerns,
            "findings": {"mocked": True, "reason": "fraud_crew out of scope for this batch"},
        }],
        "agent_call_count": 0,  # no real LLM calls made - don't count as if 3 real crew calls happened
    }


def _run_sample_claims() -> list[dict]:
    """5 real, auto_collision-only claims from data/sample_claims (India and
    non-auto incident types deliberately excluded - see CLAUDE.md/memory for
    why: never exercised by this session's redesign/calibration work).

    REAL BUG found and fixed here, live, during the first run of this
    script (2026-10-20): process_claim() uses claim_id as the LangGraph
    checkpointer's thread_id BY DESIGN (correct for real production use -
    a real claim is submitted once with a unique id, and resume_claim
    reuses that same id to continue a paused run). But these sample_claims
    fixtures (auto_accident.json, lapsed_policy.json) carry fixed,
    hardcoded claim_ids ("CLM-2024-001234", "CLM-2024-LAPSED") that had
    ALREADY been run through the real checkpointer earlier in this same
    session via scratch verification scripts (confirmed directly: the real
    src/data/claims_checkpoints.db had 10 prior checkpoint rows for
    CLM-2024-001234 before this script's first run). So the first run of
    this script silently RESUMED stale state from those earlier runs
    instead of starting fresh - visible in the trace as intake_agent
    appearing twice and several claims coming back DENIED for the wrong
    reason. Every claim_id generated here is now suffixed with a fresh
    UUID fragment so it can never collide with prior checkpoint history,
    regardless of what the source fixture's own claim_id field says."""
    import uuid
    base = Path(__file__).parent.parent / "data" / "sample_claims"
    targets = [
        ("auto_accident.json", None),
        ("lapsed_policy.json", None),
        ("test_all_paths_us.json", "path_a_normal_approved"),
        ("test_all_paths_us.json", "path_d_intake_failure_invalid_claim"),
        ("test_all_paths_us.json", "path_e_fast_mode_small_claim"),
    ]
    results = []
    for filename, key in targets:
        data = json.loads((base / filename).read_text(encoding="utf-8"))
        claim_data = data if key is None else data[key]
        claim_data = {k: v for k, v in claim_data.items() if not k.startswith("_")}
        # Always a fresh id - never trust the fixture's own hardcoded
        # claim_id as a resume key (see the real bug this fixed, above).
        claim_data["claim_id"] = f"BATCH-{uuid.uuid4().hex[:8].upper()}-{filename}-{key or 'main'}"
        for field, default in [
            ("claimant_name", "Batch Claimant"), ("claimant_email", "batch@example.com"),
            ("claimant_phone", "555-000-0000"), ("claimant_dob", "1990-01-01"),
            ("incident_location", ""), ("police_report_number", None),
            ("vehicle_year", None), ("vehicle_make", None), ("vehicle_model", None),
            ("documents", []), ("is_appeal", False), ("original_claim_id", None),
            ("type_disputed_by_claimant", False), ("type_dispute_reason", None),
        ]:
            claim_data.setdefault(field, default)

        label = f"{filename}:{key}" if key else filename
        print(f"\n{'='*70}\nRunning: {label}\n{'='*70}")
        t0 = time.time()
        claim = ClaimInput(**claim_data)
        result = g.process_claim(claim)
        elapsed = time.time() - t0
        state = result["state"]
        trace = [e.get("agent") for e in (state.get("pipeline_trace") or [])]
        entry = {
            "label": label, "paused": result["paused"],
            "final_decision": str(state.get("final_decision")),
            "final_amount_usd": state.get("final_amount_usd"),
            "pipeline_path": trace,
            "agent_call_count": state.get("agent_call_count"),
            "elapsed_s": round(elapsed, 1),
        }
        print(json.dumps(entry, indent=2, default=str))
        results.append(entry)
    return results


def _run_golden_set() -> dict:
    from evaluation.validate_judges import run_validation
    return run_validation()


# ── test_scenarios batches (30 fixtures, split into 3 sub-batches of 10) ───
# Interleaved by index (SC-01,04,07... / SC-02,05,08... / SC-03,06,09...)
# rather than split in file order, so each sub-batch gets a representative
# mix of cheap fast-mode claims, retry-heavy consistency-coordinator cases,
# and high-value confidence-gate cases - never clustering the expensive/
# risky scenarios into one batch. Split into smaller batches (not one
# 30-claim run) specifically because today's first real run needed one
# real re-run after a bug was found mid-run (see CLAUDE.md/memory) - a
# smaller batch means a bug found partway through costs less quota to
# rediscover and re-run.

# Scenarios that need an explicit resume_claim() call after process_claim()
# pauses, per each fixture's own _scenario_note (not guessed - read
# directly from data/test_scenarios/*.json): maps filename stem -> decision
# dict. SC-24 is deliberately excluded here (needs a real guardrail-halt
# config change to trigger, not a normal claim submission - see its own
# note; run separately, manually, if needed).
_RESUME_DECISIONS = {
    "SC-25_resume_reviewer_denies": {"decision": "denied", "reviewer_id": "batch-script", "notes": "batch test", "override_ai": False, "settlement_override_usd": None},
    "SC-26_resume_eval_triggered_pause": {"decision": "approved", "reviewer_id": "batch-script", "notes": "batch test", "override_ai": False, "settlement_override_usd": None},
    "SC-27_resume_prefanout_pause": {"decision": "approved", "reviewer_id": "batch-script", "notes": "batch test", "override_ai": False, "settlement_override_usd": None},
    "SC-28_resume_fanout_pause": {"decision": "approved", "reviewer_id": "batch-script", "notes": "batch test", "override_ai": False, "settlement_override_usd": None},
}

_SCENARIO_DEFAULT_FIELDS = [
    ("claimant_name", "Batch Claimant"), ("claimant_email", "batch@example.com"),
    ("claimant_phone", "555-000-0000"), ("claimant_dob", "1990-01-01"),
    ("incident_location", ""), ("police_report_number", None),
    ("vehicle_year", None), ("vehicle_make", None), ("vehicle_model", None),
    ("documents", []), ("is_appeal", False), ("original_claim_id", None),
    ("type_disputed_by_claimant", False), ("type_dispute_reason", None),
]


def _scenario_batches() -> dict[str, list[Path]]:
    base = Path(__file__).parent.parent / "data" / "test_scenarios"
    # Excludes *_documents.json companion fixtures (added later this session
    # to ground damage_assessor's assessment - see process_claim's own
    # docstring): a plain "*.json" glob picks these up as if they were their
    # own independent scenarios (real bug, caught live - see
    # 2026-10-20_batch2_scenarios3_raw.txt showing a spurious
    # "SC-19_..._documents" entry and a wrong scenarios3 membership).
    files = sorted(p for p in base.glob("*.json") if not p.stem.endswith("_documents"))
    return {
        "scenarios1": files[0::3],
        "scenarios2": files[1::3],
        "scenarios3": files[2::3],
    }


def _run_scenario_claim(path: Path) -> dict:
    import uuid
    data = json.loads(path.read_text(encoding="utf-8"))
    note = data.pop("_scenario_note", "")
    claim_data = {k: v for k, v in data.items() if not k.startswith("_")}
    # Always a fresh id, same reasoning as _run_sample_claims - never trust
    # a fixture's own claim_id as a resume key across separate script runs.
    claim_data["claim_id"] = f"BATCH-{uuid.uuid4().hex[:8].upper()}-{path.stem}"
    for field, default in _SCENARIO_DEFAULT_FIELDS:
        claim_data.setdefault(field, default)

    # Real gap found and fixed 2026-10-20 (see process_claim's own
    # docstring): nothing ever passed claim_documents through a real run
    # before, so damage_assessor's requires_physical_inspection hard rule
    # fired on almost every live scenario regardless of what the fixture
    # was meant to test, since there was never any real repair-estimate
    # content behind it. A companion SC-NN_..._documents.json (same shape
    # as data/stratified_claims/*_documents.json) is loaded here when
    # present, so scenarios that need to reach settlement/evaluator/
    # auto-reject can actually get there live, not just in the mocked
    # test suite.
    documents_path = path.with_name(path.stem + "_documents.json")
    documents = json.loads(documents_path.read_text(encoding="utf-8")) if documents_path.exists() else None

    print(f"\n{'='*70}\nRunning: {path.stem}\n{'='*70}")
    print(f"  ({note[:100]})")
    if documents:
        print(f"  (real document content attached: {list(documents.keys())})")
    t0 = time.time()
    claim = ClaimInput(**claim_data)
    result = g.process_claim(claim, documents=documents)

    resume_decision = _RESUME_DECISIONS.get(path.stem)
    if result["paused"] and resume_decision:
        print(f"  Paused as expected - resuming with decision={resume_decision['decision']!r}")
        result = g.resume_claim(claim_data["claim_id"], resume_decision)

    elapsed = time.time() - t0
    state = result["state"]
    trace = [e.get("agent") for e in (state.get("pipeline_trace") or [])]
    entry = {
        "label": path.stem, "paused": result["paused"],
        "final_decision": str(state.get("final_decision")),
        "final_amount_usd": state.get("final_amount_usd"),
        "pipeline_path": trace,
        "agent_call_count": state.get("agent_call_count"),
        "elapsed_s": round(elapsed, 1),
        "resumed": resume_decision is not None,
    }
    print(json.dumps(entry, indent=2, default=str))
    return entry


def _run_scenario_batch(batch_name: str) -> list[dict]:
    batches = _scenario_batches()
    paths = batches[batch_name]
    return [_run_scenario_claim(p) for p in paths]


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    valid_scenario_batches = {"scenarios1", "scenarios2", "scenarios3"}

    if mode == "verify":
        # One-off single-scenario check, e.g. to confirm a fixture's
        # companion _documents.json file actually changes its live outcome
        # (used to verify the process_claim(documents=...) fix).
        stem = sys.argv[2]
        with patch.object(g, "run_fraud_crew", _mock_fraud_crew):
            g._compiled_graph = None
            g._checkpointer = None
            g._checkpointer_ctx = None
            path = Path(f"data/test_scenarios/{stem}.json")
            result = _run_scenario_claim(path)
            print(json.dumps(result, indent=2, default=str))
        return

    with patch.object(g, "run_fraud_crew", _mock_fraud_crew):
        # Force a fresh graph build under the patch - get_compiled_graph's
        # module-level cache would otherwise keep whatever was built first.
        g._compiled_graph = None
        g._checkpointer = None
        g._checkpointer_ctx = None

        if mode in ("golden", "both"):
            print("\n" + "#" * 70)
            print("# GOLDEN SET (judge validation - no fraud_crew involved)")
            print("#" * 70)
            _run_golden_set()

        if mode in ("sample", "both"):
            print("\n" + "#" * 70)
            print("# SAMPLE CLAIMS (real pipeline, fraud_crew mocked)")
            print("#" * 70)
            results = _run_sample_claims()
            print("\n" + "=" * 70)
            print("SAMPLE CLAIMS SUMMARY")
            print("=" * 70)
            for r in results:
                print(f"  {r['label']:55s} paused={r['paused']!s:5s} decision={r['final_decision']:30s} "
                      f"calls={r['agent_call_count']} ({r['elapsed_s']}s)")

        if mode in valid_scenario_batches:
            print("\n" + "#" * 70)
            print(f"# TEST SCENARIOS BATCH: {mode} (real pipeline, fraud_crew mocked)")
            print("#" * 70)
            results = _run_scenario_batch(mode)
            print("\n" + "=" * 70)
            print(f"{mode.upper()} SUMMARY")
            print("=" * 70)
            for r in results:
                print(f"  {r['label']:55s} paused={r['paused']!s:5s} decision={r['final_decision']:30s} "
                      f"calls={r['agent_call_count']} resumed={r['resumed']} ({r['elapsed_s']}s)")


if __name__ == "__main__":
    main()
