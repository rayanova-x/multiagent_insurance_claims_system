"""Offline evaluation results for the EvalsDashboard.

Deliberately narrow scope (2026-10-20): this project has two kinds of
eval/test runs, and only ONE kind belongs on this dashboard right now.

1. INTERNAL FLOW VERIFICATION - the 31-branch architectural coverage
   (data/test_scenarios/, 30 hand-built fixtures, one per routing branch)
   plus the guardrail-halt pair run separately
   (data/batch_results/2026-10-20_batch2_scenarios1/2/3.json,
   2026-10-20_branch7_24_guardrail_halt.json) and the judge-calibration
   golden set (2026-10-20_batch1_golden_and_sample.json). These are REAL,
   CLEAN, already fully live-verified this session - every branch reached
   and checked against actual pipeline state, not partial or
   quota-contaminated. This is what this endpoint surfaces.

2. TASK-COMPLETION-RATE EVAL (the 100-claim rule-driven test set,
   data/test_set_100/ + scripts/run_100_test_set.py) - explicitly NOT
   surfaced here yet. Only 1 of 20 batches has run so far, and that one
   batch was itself contaminated by a real Gemini daily-quota exhaustion
   incident (see CLAUDE.md's rebuild-status log and project memory
   project-smart-claims-100-rule-driven-test-set for the full story) -
   showing a number from it would misrepresent an incomplete, noisy
   partial result as a trustworthy finished one. Per explicit user
   decision: this dashboard shows NOTHING for this eval until the full,
   clean 100-claim run actually completes - at which point this file
   should be extended to read scripts/run_100_test_set.py's real output
   files and compute the genuine completion rate.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends

from api.security import require_role

router = APIRouter(
    prefix="/api/evals",
    tags=["Evals"],
    dependencies=[Depends(require_role("reviewer", "admin"))],
)

_BATCH_RESULTS_DIR = Path(__file__).resolve().parent.parent / "data" / "batch_results"

# The exact, real files from this session's internal-flow verification -
# an explicit allowlist, not a glob, so a future unrelated file dropped
# into data/batch_results/ can never silently start appearing on this
# dashboard without a deliberate code change.
_FLOW_VERIFICATION_FILES = [
    "2026-10-20_batch1_golden_and_sample.json",
    "2026-10-20_batch2_scenarios1.json",
    "2026-10-20_batch2_scenarios2.json",
    "2026-10-20_batch2_scenarios3.json",
    "2026-10-20_branch7_24_guardrail_halt.json",
]


def _summarize_judge_calibration(data: dict, fname: str) -> dict:
    gs = data["golden_set_results"]
    dims = {k: v for k, v in gs.items() if isinstance(v, dict) and "agreement" in v}
    return {
        "file": fname,
        "kind": "judge_calibration",
        "date": data.get("date"),
        "overall": gs.get("overall"),
        "dimensions": {
            name: {
                "agreement": info.get("agreement"),
                "disagreement_count": len(info.get("disagreements") or []),
            }
            for name, info in dims.items()
        },
    }


def _summarize_live_batch(data: dict, fname: str) -> dict:
    results = data.get("results", [])
    paused = sum(1 for r in results if r.get("paused"))
    return {
        "file": fname,
        "kind": "live_batch",
        "date": data.get("date"),
        "batch": data.get("batch"),
        "total_claims": len(results),
        "paused_count": paused,
        "completed_count": len(results) - paused,
    }


@router.get("/flow-verification")
def flow_verification():
    """The 30-claim (+ guardrail-halt pair + golden set) internal routing
    verification results - real, clean, already fully live-verified this
    session. Returns an empty list (not an error) if a file is missing,
    so this endpoint degrades gracefully rather than erroring on a fresh
    checkout that hasn't run these yet."""
    summaries = []
    total_claims = 0
    total_paused = 0

    for fname in _FLOW_VERIFICATION_FILES:
        path = _BATCH_RESULTS_DIR / fname
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if "golden_set_results" in data:
            summaries.append(_summarize_judge_calibration(data, fname))
        elif "results" in data:
            summary = _summarize_live_batch(data, fname)
            summaries.append(summary)
            total_claims += summary["total_claims"]
            total_paused += summary["paused_count"]

    return {
        "files": summaries,
        "total_claims_verified": total_claims,
        "total_paused": total_paused,
        "total_completed": total_claims - total_paused,
        "note": (
            "31 architectural branches live-verified with real LLM calls "
            "(data/test_scenarios/, 30 fixtures + guardrail-halt pair). "
            "Task-completion-rate eval (the 100-claim rule-driven test set) "
            "is not yet shown here - only 1 of 20 batches has run, and it "
            "was contaminated by real quota exhaustion. This section will "
            "be extended once that full run completes cleanly."
        ),
    }
