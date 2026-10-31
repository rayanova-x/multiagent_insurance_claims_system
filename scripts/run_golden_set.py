"""Runs the 20-claim demo golden set (data/golden_set/) against the real
pipeline (real LLM calls; fraud_crew mocked inert, same as run_live_batch.py).

Specs are relative (vehicle age, repair/ACV ratio, days-ago) and materialized
for today by src/data/golden.py, so the same scenarios keep testing the same
behaviour as the calendar moves. Golden policies (POL-GOLD-NN) are upserted
into data/policies.db with windows relative to today.

Usage:
    python scripts/run_golden_set.py            # all 20
    python scripts/run_golden_set.py 6 7 13     # only GOLD-06, 07, 13 (ids or numbers)
    python scripts/run_golden_set.py --pairs    # the 20, plus the 3 fairness pairs (6 more claims)
    python scripts/run_golden_set.py --pairs-only
    python scripts/run_golden_set.py --dry      # materialize + print, no LLM calls

Every finished claim is appended to <date>_golden_set_<model>.partial.jsonl right away, and a re-run
resumes from it automatically (claims that errored are retried); --fresh discards it and starts over.

Exit code 1 when the gate fails (any critical error, or tier-1 pass rate below
TIER1_TARGET in src/data/golden.py) so it can block a build. A fairness-pair
mismatch also fails the gate.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.trajectory import _retry_recovery_status
from src.agents import graph as g
from src.data.golden import (
    compare_pair, gate_failures, load_pairs, load_specs, materialize, materialize_pair, score_result, summarize,
    seed_policy as _seed_policy,
)
from src.config import get_llm_config
from src.llm import get_token_usage
from src.models.state import ClaimInput
from scripts.run_live_batch import _mock_fraud_crew


def _load_partial(path: Path) -> dict:
    """Rows saved by an earlier (possibly crashed) run today: {(kind, id): row}. A torn last line is ignored."""
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            done[(row["kind"], row["id"])] = row
    return done


def _append_partial(path: Path, row: dict) -> None:
    """Persist one finished claim/pair immediately (flush + fsync) so a crash loses at most the one in flight."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _observe(result: dict, initial: dict, models: dict) -> dict:
    state = result["state"]
    decision = state.get("final_decision")
    damage = state.get("damage_output")
    letter = state.get("communication_output")
    return {
        "paused_initial": initial["paused"],
        "paused_final": result["paused"],
        "triggers": (initial.get("interrupt") or {}).get("triggers") or [],
        "final_decision": (decision.value if hasattr(decision, "value") else decision) if decision else None,
        "final_amount": state.get("final_amount_usd"),
        "agents": [e.get("agent") for e in (state.get("pipeline_trace") or [])],
        "assessed_damage": getattr(damage, "assessed_damage_usd", None),
        "repair_vs_replace": getattr(damage, "repair_vs_replace", None),
        "cost_usd": float(state.get("total_cost_usd") or 0.0),
        "tokens": int(state.get("total_tokens_used") or 0),
        "seconds": float(state.get("processing_seconds") or 0.0),
        "models": models,
        "agent_call_count": int(state.get("agent_call_count") or 0),
        "guardrails_halted": bool(state.get("guardrails_halted")),
        "consistency_retry_counts": dict(state.get("consistency_retry_counts") or {}),
        "consistency_has_conflicts": getattr(state.get("consistency_output"), "has_conflicts", None),
        "retry_status": _retry_recovery_status(state),
        # None when the claim ended paused (no letter yet); True when the accuracy/safety backstop replaced the
        # model's letter with the template, i.e. the model's own letter would have been wrong or unsafe.
        "letter_overridden": None if letter is None else any(m in (letter.internal_notes or "") for m in ("ACCURACY OVERRIDE", "SAFETY OVERRIDE")),
        "letter": None if letter is None else {"subject": letter.subject, "message": letter.message[:1500], "internal_notes": (letter.internal_notes or "")[:600]},
    }


def _raise_on_crash(result: dict) -> None:
    """process_claim() catches a pipeline crash and returns a normal-looking "escalated" state (empty trace). Scored as
    data it would look like a real outcome - seen live when the machine slept mid-run - so surface it as an error
    instead; the runner records errors and retries them on resume."""
    crash = [e for e in (result["state"].get("error_log") or []) if str(e).startswith("Pipeline crash")]
    if crash:
        raise RuntimeError(crash[0])


def _run_one(m: dict) -> tuple[dict, dict]:
    """Run one materialized claim (plus its reviewer resume if the spec has one). Returns (observation, result)."""
    claim = dict(m["claim"])
    claim["claim_id"] = f"{m['id']}-{uuid.uuid4().hex[:6].upper()}"  # fresh id: never resume stale checkpoints
    initial = g.process_claim(ClaimInput(**claim), documents=m["documents"])
    _raise_on_crash(initial)
    models = dict(get_token_usage()["models"])
    result = initial
    if initial["paused"] and m["spec"].get("resume"):
        result = g.resume_claim(claim["claim_id"], m["spec"]["resume"])  # resets the per-run usage counter
        _raise_on_crash(result)
        for name, n in get_token_usage()["models"].items():
            models[name] = models.get(name, 0) + n
    return _observe(result, initial, models), result


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    pairs_only = "--pairs-only" in sys.argv
    with_pairs = pairs_only or "--pairs" in sys.argv
    wanted = {f"GOLD-{int(a):02d}" if a.isdigit() else a for a in args}
    specs = load_specs()
    by_id = {s["id"]: s for s in specs}
    items = [] if pairs_only else [materialize(s, date.today()) for s in specs if not wanted or s["id"] in wanted]
    pair_items = [(p, *materialize_pair(p, by_id, date.today())) for p in load_pairs()] if with_pairs else []

    if dry:
        for m in items:
            print(m["id"], m["spec"]["slug"], json.dumps(m["oracle"]))
        for p, a, b in pair_items:
            ca, cb = a["claim"], b["claim"]
            print(p["id"], "->", ca["claimant_name"], "/", ca["incident_location"], "vs", cb["claimant_name"], "/", cb["incident_location"])
        return

    cfg = get_llm_config()
    partial = Path("data/batch_results") / f"{date.today().isoformat()}_golden_set_{cfg['model']}.partial.jsonl"
    if "--fresh" in sys.argv and partial.exists():
        partial.unlink()
    done = _load_partial(partial)
    if done:
        print(f"Resuming: {len(done)} item(s) already saved in {partial} (use --fresh to start over)")

    results, pair_results = [], []
    with patch.object(g, "run_fraud_crew", _mock_fraud_crew):
        g._compiled_graph = None
        g._checkpointer = None
        g._checkpointer_ctx = None
        for m in items + [x for _, a, b in pair_items for x in (a, b)]:
            if m["policy"]:
                _seed_policy(m["policy"])

        for m in items:
            prior = done.get(("claim", m["id"]))
            if prior and prior.get("status") in ("scored", "observed"):
                print(f"\n{m['id']} {m['spec']['slug']}: already saved, skipping")
                results.append(prior)
                continue
            print(f"\n{'='*70}\n{m['id']} {m['spec']['slug']}\n{'='*70}")
            t0 = time.time()
            try:
                obs, result = _run_one(m)
                scored = score_result(m, obs)
                scored.update(observed=obs, elapsed_s=round(time.time() - t0, 1), label_basis=m["spec"]["label_basis"])
                if scored["status"] == "observed":
                    scored["consistency"] = str(result["state"].get("consistency_output"))[:600]
                for name, ok, detail in scored["checks"]:
                    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {'' if ok else detail}")
                print(f"  tier1={scored['tier1']} tier2={scored['tier2']} status={scored['status']} errors={scored.get('errors')}")
            except Exception as e:  # keep going; a crash is a recorded failure, never silently dropped
                scored = {"id": m["id"], "status": "error", "error": str(e), "tier1": False, "tier2": None, "checks": [], "errors": []}
                print(f"  ERROR: {e}")
            results.append(scored)
            _append_partial(partial, {"kind": "claim", **scored})

        for p, a, b in pair_items:
            prior = done.get(("pair", p["id"]))
            if prior and "error" not in prior:
                print(f"\n{p['id']}: already saved, skipping")
                pair_results.append(prior)
                continue
            print(f"\n{'='*70}\n{p['id']} (base {p['base']}): {p['why']}\n{'='*70}")
            try:
                obs_a, _ = _run_one(a)
                obs_b, _ = _run_one(b)
                checks = compare_pair(obs_a, obs_b)
                for name, ok, detail in checks:
                    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {'' if ok else detail}")
                pair_results.append({"id": p["id"], "ok": all(ok for _, ok, _ in checks), "checks": checks, "a": obs_a, "b": obs_b})
            except Exception as e:
                pair_results.append({"id": p["id"], "ok": False, "error": str(e), "checks": []})
                print(f"  ERROR: {e}")
            _append_partial(partial, {"kind": "pair", **pair_results[-1]})

    summary = summarize(results)
    summary["pairs"] = {"ran": len(pair_results), "consistent": sum(1 for r in pair_results if r["ok"])}
    failures = gate_failures(summary) if results else []
    if summary["pairs"]["consistent"] != summary["pairs"]["ran"]:
        failures.append(f"fairness pairs: {summary['pairs']['consistent']}/{summary['pairs']['ran']} consistent")
    summary.update(
        date=date.today().isoformat(), primary_model=cfg["model"], judge_model=cfg.get("judge_model"),
        claims=len(results), gate_failures=failures,
        observed_only=[r["id"] for r in results if r["status"] == "observed"],
        failed=[r["id"] for r in results if r["status"] in ("scored", "error") and (not r["tier1"] or r["tier2"] is False)],
    )
    out = Path("data/batch_results") / f"{date.today().isoformat()}_golden_set_{cfg['model']}.json"
    out.write_text(json.dumps({**summary, "results": results, "pair_results": pair_results}, indent=2, default=str), encoding="utf-8")
    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    print(json.dumps(summary, indent=2, default=str))
    print(f"Saved to {out}")
    partial.unlink(missing_ok=True)  # everything is in the final file now
    if failures:
        print("\nGATE FAILED:", *failures, sep="\n  - ")
        sys.exit(1)
    print("\nGATE PASSED")


if __name__ == "__main__":
    main()
