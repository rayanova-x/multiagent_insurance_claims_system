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

from fastapi import APIRouter, Depends, HTTPException

from api.security import require_role

router = APIRouter(
    prefix="/api/evals",
    tags=["Evals"],
    dependencies=[Depends(require_role("reviewer", "admin"))],
)

_BATCH_RESULTS_DIR = Path(__file__).resolve().parent.parent / "data" / "batch_results"
_TEST_PDFS_DIR = Path(__file__).resolve().parent.parent / "data" / "test_pdfs"

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


# ── Run-any-case (golden set + test_pdfs), for the Evals tab's "Run this
# case" button. Reuses the real claim-submission path - this endpoint only
# returns data to pre-fill the form with, it never calls process_claim()
# itself (see api/routes_claims.py's /submit, which still does the real
# submission once the frontend has pre-filled from here). ──────────────────

def _load_test_pdfs_manifest() -> dict:
    path = _TEST_PDFS_DIR / "cases.json"
    if not path.exists():
        return {"claim": {}, "cases": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"claim": {}, "cases": []}


@router.get("/cases")
def list_cases():
    """All runnable eval cases - golden-set specs (data/golden_set/GOLD-*.json)
    plus test_pdfs cases (data/test_pdfs/cases.json) - for the Evals tab's
    per-case table. A thin listing only; materializing one case's full form
    data happens in /cases/{id}/materialize below."""
    from src.data.golden import load_specs

    cases = []
    for spec in load_specs():
        cases.append({
            "id": spec["id"],
            "source": "golden_set",
            "label": spec.get("slug", spec["id"]).replace("_", " ").title(),
            "description": spec.get("rule_citation", ""),
        })

    manifest = _load_test_pdfs_manifest()
    for case in manifest.get("cases", []):
        cases.append({
            "id": case["id"],
            "source": "test_pdfs",
            "label": case["label"],
            "description": case.get("description", ""),
        })

    return {"cases": cases}


@router.get("/cases/{case_id}/materialize")
def materialize_case(case_id: str):
    """Concrete form data (and, for golden-set cases, already-structured
    document content) for one case, used to pre-fill SubmitClaim.jsx.

    golden_set: calls src.data.golden.materialize() (the same function
    scripts/run_golden_set.py uses for real live runs) and seeds the
    case's synthetic policy so a submitted claim referencing it resolves -
    same seed_policy() helper the CLI script uses, not a second
    implementation. Returns the already-structured `documents` dict
    (AutoCollisionDocuments shape, not PDF files) passed straight through
    on submit via SubmitClaimRequest.prebuilt_documents - a deliberate,
    documented exception to the real-upload path, since golden-set cases
    don't have PDF files to stage (see this round's plan for why: passing
    the structured content preserves what each case is actually designed
    to test, e.g. a case assuming a repair estimate is present).

    test_pdfs: returns form fields + which PDF file(s) to stage into which
    slot(s) - the frontend stages these via the REAL
    /upload-staging/{token}/{slot} endpoints, same as a live claimant.
    """
    if case_id.startswith("GOLD-"):
        from src.data.golden import load_specs, materialize, seed_policy

        spec = next((s for s in load_specs() if s["id"] == case_id), None)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown case: {case_id}")

        m = materialize(spec)
        if m.get("policy"):
            seed_policy(m["policy"])

        return {
            "source": "golden_set",
            "claim": m["claim"],
            "prebuilt_documents": m.get("documents"),
            "pdf_files": None,
        }

    manifest = _load_test_pdfs_manifest()
    case = next((c for c in manifest.get("cases", []) if c["id"] == case_id), None)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Unknown case: {case_id}")

    return {
        "source": "test_pdfs",
        "claim": manifest.get("claim", {}),
        "prebuilt_documents": None,
        "pdf_files": case.get("files", {}),
    }


@router.get("/cases/{case_id}/pdf/{slot}")
def get_case_pdf(case_id: str, slot: str):
    """Serves one test_pdfs case's PDF file by slot, for the frontend to
    download and re-upload through the real staging endpoints (mirrors how
    a claimant would pick a file from their own disk - the browser just
    gets it from here instead)."""
    manifest = _load_test_pdfs_manifest()
    case = next((c for c in manifest.get("cases", []) if c["id"] == case_id), None)
    if case is None or slot not in case.get("files", {}):
        raise HTTPException(status_code=404, detail="Unknown case or slot")

    from fastapi.responses import FileResponse

    path = _TEST_PDFS_DIR / case["files"][slot]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(str(path), media_type="application/pdf", filename=path.name)
