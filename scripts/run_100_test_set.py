"""Runs the 17-category, 100-claim rule-driven test set
(data/test_set_100/, built by src/data/generate.py:generate_100_test_set())
against the real live pipeline (real Gemini calls, fraud_crew mocked per
this session's standing scope decision - see scripts/run_live_batch.py's
own _mock_fraud_crew, reused directly here).

Each category has ONE real, rule-derived definition of a correct outcome,
grounded in this project's own actual enforced rules (not invented) - see
_expected_outcome()'s own docstring for the full mapping, and
src/data/selection.py:build_100_test_set()'s own docstring for how each
category's claim content was constructed.

Usage:
    python scripts/run_100_test_set.py            # all 100
    python scripts/run_100_test_set.py 10 1        # batch 1 of 10 (claims 1-10)
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agents import graph as g
from src.models.state import ClaimInput
from scripts.run_live_batch import _mock_fraud_crew


def _expected_outcome(tag: str) -> str:
    """Maps each of the 17 categories to its ONE correct real outcome, per
    this project's own actually-enforced rules (src/agents/graph.py's
    routing, src/agents/*.py's hard rules) - not invented per-claim like
    scripts/run_stratified_eval.py had to do retroactively, since every
    claim here was purpose-built for exactly one outcome from the start.
    """
    return {
        "clean": "settled_not_denied",
        "high_value_clean": "paused_for_review",  # the $84,100 value gate always needs sign-off
        "lapsed_policy": "denied_at_intake",
        "exclusion_racing": "paused_for_review",
        "exclusion_racing_near_miss": "settled_not_denied",
        "exclusion_commercial_use": "paused_for_review",
        "exclusion_commercial_use_near_miss": "settled_not_denied",
        "exclusion_pre_existing_damage": "paused_for_review",
        "exclusion_pre_existing_damage_near_miss": "settled_not_denied",
        "missing_doc_repair_estimate": "paused_for_inspection",
        "missing_doc_other": "settled_not_denied",
        "disputed_incident_type": "paused_for_review",
        "prompt_injection": "paused_for_review",
        "invalid_policy_number": "denied_at_intake",
        "fast_mode_small_claim": "settled_not_denied",
        "low_confidence_high_value": "paused_for_review",  # confidence gate fires on high-value
        "low_confidence_low_value": "settled_not_denied",  # must NOT pause on confidence alone
        # Best-effort, judge-dependent - not graded pass/fail against ONE
        # fixed expected outcome (see build_100_test_set()'s own docstring:
        # "not guaranteed, depends on the live judge's own read"). Recorded
        # as "observed" only, same honest treatment as test_scenarios'
        # SC-10/11/12 this session.
        "conflict_material_retry": None,
        "conflict_notable_no_retry": None,
    }[tag]


def _actual_outcome(result: dict) -> tuple[str, str]:
    state = result["state"]
    if result["paused"]:
        interrupt = result.get("interrupt") or {}
        triggers = interrupt.get("triggers") or []
        if any("physical inspection" in t.lower() for t in triggers):
            return "paused_for_inspection", f"triggers={triggers}"
        return "paused_for_review", f"triggers={triggers}"

    decision = state.get("final_decision")
    decision_str = decision.value if hasattr(decision, "value") else str(decision)
    amount = state.get("final_amount_usd")
    intake_output = state.get("intake_output")
    is_valid = getattr(intake_output, "is_valid", None) if intake_output else None

    if decision_str == "denied" and len(state.get("pipeline_trace") or []) <= 2 and is_valid is False:
        return "denied_at_intake", f"decision={decision_str}, amount={amount}"
    if decision_str in ("approved", "approved_partial", "denied"):
        return "settled_not_denied" if decision_str != "denied" else "denied_or_zero_settlement", \
            f"decision={decision_str}, amount={amount}"
    return "other", f"decision={decision_str}, amount={amount}, paused={result['paused']}"


def main() -> None:
    base = Path(__file__).parent.parent / "data" / "test_set_100"
    claim_files = sorted(base.glob("*_claim.json"))
    # Claims are grouped by category in file order (all "clean" first, then
    # all "high_value_clean", etc. - see src/data/selection.py:
    # build_100_test_set()'s own generation order), so a sequential slice
    # would be badly stratified (confirmed: claims 1-10 are 6 clean + 4
    # high_value_clean, zero of the other 15 categories). Interleaved by
    # index (files[0::num_batches], files[1::num_batches], ...), same
    # reasoning as this session's earlier scenarios1/2/3 split, so each
    # batch samples across every category instead of running one category
    # at a time. num_batches is DERIVED from the requested batch_size
    # (100 // batch_size), not hardcoded to 10 - a real bug fixed
    # 2026-10-20: batch_size was accepted as a CLI arg but never actually
    # used to compute the interleave width, so "batch_size 5" would have
    # silently still produced 10-claim batches.
    batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else None
    batch_num = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    num_batches = (len(claim_files) // batch_size) if batch_size else 1
    interleaved = [claim_files[i::num_batches] for i in range(num_batches)]

    out_name = f"2026-10-20_100_test_set_batch{batch_num}.json" if batch_size else \
        "2026-10-20_100_test_set_full.json"
    if batch_size:
        claim_files = interleaved[batch_num - 1]
        print(f"Running batch {batch_num} of {num_batches} (interleaved, {len(claim_files)} claims)")

    results = []
    with patch.object(g, "run_fraud_crew", _mock_fraud_crew):
        g._compiled_graph = None
        g._checkpointer = None
        g._checkpointer_ctx = None

        for path in claim_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            tag = data.get("_selection_tag", "")
            stem = path.stem.replace("_claim", "")
            claim_data = {k: v for k, v in data.items() if not k.startswith("_")}
            claim_data["claim_id"] = f"T100-{uuid.uuid4().hex[:8].upper()}-{stem}"

            docs_path = path.with_name(stem + "_documents.json")
            documents = json.loads(docs_path.read_text(encoding="utf-8")) if docs_path.exists() else None

            print(f"\n{'='*70}\n{stem} (tag={tag})\n{'='*70}")
            t0 = time.time()
            try:
                claim = ClaimInput(**claim_data)
                result = g.process_claim(claim, documents=documents)
                actual, detail = _actual_outcome(result)
                expected = _expected_outcome(tag)
                correct = (expected == actual) if expected is not None else None
                elapsed = round(time.time() - t0, 1)
                entry = {
                    "claim": stem, "tag": tag, "expected": expected, "actual": actual,
                    "correct": correct, "detail": detail, "elapsed_s": elapsed,
                }
                print(json.dumps(entry, indent=2))
                results.append(entry)
            except Exception as e:
                print(f"  ERROR: {e}")
                results.append({"claim": stem, "tag": tag, "error": str(e)})

    scoreable = [r for r in results if r.get("correct") is not None]
    correct_count = sum(1 for r in scoreable if r["correct"])
    best_effort = [r for r in results if r.get("correct") is None and "error" not in r]

    by_tag = {}
    for r in results:
        t = r["tag"]
        by_tag.setdefault(t, {"correct": 0, "total": 0, "best_effort": 0})
        by_tag[t]["total"] += 1
        if r.get("correct") is True:
            by_tag[t]["correct"] += 1
        elif r.get("correct") is None and "error" not in r:
            by_tag[t]["best_effort"] += 1

    summary = {
        "total_claims": len(results),
        "scoreable_claims": len(scoreable),
        "correct": correct_count,
        "completion_rate": round(correct_count / len(scoreable), 3) if scoreable else 0.0,
        "best_effort_claims_not_scored": len(best_effort),
        "by_tag": by_tag,
        "results": results,
    }
    out_path = Path("data/batch_results") / out_name
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\n\n{'='*70}\nSUMMARY\n{'='*70}")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
