"""Model-swap regression gate (baswe.ai evaluation field guide ss2.5
"regression testing on model swaps" + ss6.5 "silent drift on hosted
models"): "when you change model or even just the provider's version,
rerun your entire eval set before you trust it... never swap a model on
the strength of a leaderboard or a launch blog. Swap it because it beat
your baseline on your data."

Reuses src/provenance.py's existing run_identity()/config_fingerprint()
(already live, tested, wired into every agent call via guard_node - see
tests/test_provenance.py) rather than inventing a second way to identify
what produced a run: a "baseline" here is the most recent eval_history.jsonl
entry recorded under a DIFFERENT model/config fingerprint than right now -
i.e. the last known-good result before whatever model/config swap is being
tested. Comparing against the immediately preceding same-config entry would
just measure LLM output noise, not a real swap's effect.

Usage:
    python evaluation/regression_check.py                     # run + compare + record
    python evaluation/regression_check.py --no-record          # run + compare, don't append to history
    python evaluation/regression_check.py --threshold 0.05     # custom max allowed score drop

Exits non-zero (fails the calling CI job) when avg_eval_score or
eval_pass_rate drops by more than --threshold versus the last real
baseline. A missing baseline (first-ever run, or every prior entry shares
today's exact model/config) is reported but does not fail - there is
nothing to regress against yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.run_eval import run_batch_evaluation
from src.provenance import run_identity

HISTORY_PATH = Path(__file__).parent / "eval_history.jsonl"

# Metrics checked for regression, and which direction is "worse". Both are
# already computed by run_batch_evaluation's summary - nothing re-derived.
_TRACKED_METRICS = ["avg_eval_score", "eval_pass_rate"]


def _read_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    entries = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def _append_history(entry: dict) -> None:
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _identity_key(entry: dict) -> tuple:
    return (entry.get("model"), entry.get("config_sha256"))


def find_baseline(history: list[dict], current_identity: dict) -> dict | None:
    """Most recent entry whose (model, config_sha256) differs from the
    identity running right now - the last real baseline before today's
    model/config, if any swap has actually happened. Entries under the
    SAME identity as now are same-config noise, not a baseline to compare
    against (comparing today's run to yesterday's run of the identical
    model/config would measure LLM call variance, not a swap's effect)."""
    current_key = (current_identity.get("model"), current_identity.get("config_sha256"))
    for entry in reversed(history):
        if _identity_key(entry) != current_key:
            return entry
    return None


def run_regression_check(
    claims_dir: str = "data/sample_claims",
    country: str | None = None,
    threshold: float = 0.05,
    record: bool = True,
) -> dict:
    identity = run_identity()
    print(f"Running batch evaluation under: model={identity['model']} "
          f"provider={identity['provider']} config_sha256={identity['config_sha256']}")

    eval_result = run_batch_evaluation(claims_dir, country=country)
    summary = eval_result.get("summary", {})

    history = _read_history()
    baseline = find_baseline(history, identity)

    print("\n" + "=" * 60)
    print("MODEL-SWAP REGRESSION CHECK")
    print("=" * 60)

    regressions: list[str] = []
    if baseline is None:
        print("No prior baseline under a different model/config found - nothing to compare "
              "against yet (first run, or every history entry shares this exact model/config).")
    else:
        print(f"Baseline: model={baseline.get('model')} config_sha256={baseline.get('config_sha256')} "
              f"(recorded {baseline.get('timestamp')})")
        for metric in _TRACKED_METRICS:
            current_val = summary.get(metric)
            baseline_val = baseline.get(metric)
            if current_val is None or baseline_val is None:
                print(f"  {metric}: skipped (missing on one side - current={current_val}, baseline={baseline_val})")
                continue
            delta = current_val - baseline_val
            status = "REGRESSION" if delta < -threshold else "ok"
            print(f"  {metric}: {baseline_val:.3f} -> {current_val:.3f} (delta {delta:+.3f}) [{status}]")
            if status == "REGRESSION":
                regressions.append(
                    f"{metric} dropped {-delta:.3f} (from {baseline_val:.3f} to {current_val:.3f}), "
                    f"exceeding the allowed {threshold:.3f}"
                )

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **identity,
        **{k: summary.get(k) for k in _TRACKED_METRICS},
        "total_claims": summary.get("total_claims"),
        "total_cost_usd": summary.get("total_cost_usd"),
    }
    if record:
        _append_history(entry)
        print(f"\nRecorded this run to {HISTORY_PATH}")

    print("=" * 60)
    if regressions:
        print("RESULT: REGRESSION DETECTED")
        for r in regressions:
            print(f"  - {r}")
    else:
        print("RESULT: no regression detected")

    return {"summary": summary, "identity": identity, "baseline": baseline, "regressions": regressions}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--claims-dir", default="data/sample_claims")
    parser.add_argument("--country", choices=["us", "india"], default=None)
    parser.add_argument("--threshold", type=float, default=0.05,
                         help="Max allowed drop in avg_eval_score/eval_pass_rate before this fails (default 0.05)")
    parser.add_argument("--no-record", action="store_true", help="Don't append this run to eval_history.jsonl")
    args = parser.parse_args()

    result = run_regression_check(
        claims_dir=args.claims_dir, country=args.country,
        threshold=args.threshold, record=not args.no_record,
    )
    sys.exit(1 if result["regressions"] else 0)
