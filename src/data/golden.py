"""Demo golden set: time-proof claim specs -> concrete claims.

A spec (data/golden_set/GOLD-NN_*.json) describes a scenario by INTENT, not by
calendar values: "vehicle is 4 years old, repair is 40% of its ACV, incident
was 45 days ago, policy is mid-term". materialize() turns that into a concrete
claim + documents + policy for a given `today`. Because the vehicle's AGE is
pinned (vehicle_year = incident year - age) and production measures age at the
incident date (calculate_vehicle_acv / calculate_asset_age take as_of=incident_date),
the ACV bracket, depreciation rate and repair/ACV ratio never change as the
calendar moves or when the claim is processed, so the scenario keeps testing the
behaviour it was written for.

The payout oracle below is deliberately an independent re-implementation of the
published rates (CLAUDE.md / damage_calculator.py), not a call into production
code; tests/test_golden_set.py cross-checks the two against each other.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.config import get_agent_config

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "data" / "golden_set"

# ── Independent oracle: published ACV brackets and depreciation rates ───────
_ACV_BRACKETS = [(1, 35000), (3, 28000), (6, 20000), (10, 13000), (15, 7000), (20, 3500), (30, 1500)]
_TOTAL_LOSS_THRESHOLD = 0.75
_COVERAGE = {
    "collision": 50000, "comprehensive": 50000, "liability": 100000,
    "medical_payments": 5000, "uninsured_motorist": 50000, "rental_reimbursement": 1500,
}
# kind -> (deductible, exclusions); same shapes as scripts/seed_policies.py's US auto policies.
_POLICY_KINDS = {
    "standard": (500, ["racing", "commercial_use"]),
    "highded": (15000, ["racing", "commercial_use"]),
    "preexisting": (1000, ["racing", "commercial_use", "pre_existing_damage"]),
    "lowded": (100, ["racing", "commercial_use"]),
}
DEFAULT_WINDOW = {"start_days_ago": 200, "end_days_ahead": 165}
ALL_DOCS = ["police_report", "other_driver_info", "repair_estimate"]
HIGH_VALUE_THRESHOLD = 84100  # configs/countries/us.yaml hitl.triggers.min_amount


def acv_for_age(age: int) -> float:
    for ceiling, value in _ACV_BRACKETS:
        if age <= ceiling:
            return float(value)
    raise ValueError(f"golden specs only cover vehicles up to 30 years old, got {age}")


def depreciation_for(damage: float, age: int) -> float:
    rate = 0.20 if age <= 1 else 0.15 if age <= 2 else 0.12 if age <= 3 else 0.09 if age <= 5 else 0.07
    return round(min(damage * rate * age, damage * 0.70), 2)


def expected_payout(assessed: float, age: int, deductible: float, limit: float = _COVERAGE["collision"]) -> float:
    """gross - depreciation - deductible, floored at 0 and capped at the coverage limit
    (the formula settlement_calculator's prompt states and its fallback applies)."""
    return round(max(0.0, min(assessed - depreciation_for(assessed, age) - deductible, limit)), 2)


# ── Spec loading ─────────────────────────────────────────────────────────────

def load_specs() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(GOLDEN_DIR.glob("GOLD-*.json"))]


def seed_policy(policy: dict) -> None:
    """Upsert a materialized golden-spec policy into the real policy DB so a
    submitted claim referencing it resolves. Shared by scripts/run_golden_set.py
    and api/routes_evals.py's case-run endpoint - one implementation, not two."""
    from sqlalchemy import text
    from src.tools.policy_lookup import _ensure_db, _get_engine

    _ensure_db()
    with _get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO policies (policy_number, data) VALUES (:num, :data) "
                 "ON CONFLICT (policy_number) DO UPDATE SET data = :data"),
            {"num": policy["policy_number"], "data": json.dumps(policy)},
        )


# ── Materialization ──────────────────────────────────────────────────────────

def _split_items(total: float, items: list[list]) -> list[dict]:
    """Line items from [description, share] pairs; the last absorbs rounding so the sum is exact."""
    out, running = [], Decimal("0")
    for i, (desc, share) in enumerate(items):
        amt = Decimal(str(total)) - running if i == len(items) - 1 else (Decimal(str(total)) * Decimal(str(share))).quantize(Decimal("0.01"))
        running += amt
        out.append({"description": desc, "amount": f"{amt:.2f}"})
    return out


def _documents(spec: dict, claim: dict, repair_total: float | None) -> dict | None:
    present = spec.get("documents", ALL_DOCS)
    docs: dict[str, Any] = {}
    if "police_report" in present:
        docs["police_report"] = {
            "report_number": claim["police_report_number"], "jurisdiction": spec.get("jurisdiction", "Local Police Department"),
            "incident_date": claim["incident_date"], "incident_location": claim["incident_location"],
            "narrative": claim["incident_description"], "fault_determination": spec.get("fault", "Other driver cited as at fault"),
            "citations_issued": [], "parties_involved": [claim["claimant_name"], spec.get("other_driver", "Alex Morgan")],
        }
    if "other_driver_info" in present:
        docs["other_driver_info"] = {
            "driver_name": spec.get("other_driver", "Alex Morgan"), "driver_license_number": "D5521907", "insurer_name": "Pinnacle Mutual",
            "policy_number": None, "vehicle_make": "Toyota", "vehicle_model": "Corolla", "vehicle_year": 2020, "license_plate": None,
        }
    if "repair_estimate" in present and repair_total is not None:
        items = _split_items(repair_total, spec.get("repair", {}).get("items") or [["Parts and materials", 0.6], ["Labor", 0.4]])
        docs["repair_estimate"] = {
            "shop_name": "Precision Collision Center", "estimate_date": claim["incident_date"], "line_items": items,
            "line_items_sum": f"{sum(Decimal(i['amount']) for i in items):.2f}", "stated_total": f"{repair_total:.0f}" if repair_total == int(repair_total) else f"{repair_total:.2f}",
        }
    return docs or None


def materialize(spec: dict, today: date | None = None) -> dict:
    """Concrete {claim, documents, policy, oracle, expected} for `today` (default: the real today)."""
    today = today or date.today()
    veh = spec["vehicle"]
    age = veh["age_years"]
    acv = acv_for_age(age)
    incident = today - timedelta(days=spec["incident_days_ago"])

    repair = spec.get("repair")
    if repair and "ratio_of_acv" in repair:
        repair_total: float | None = float(round(repair["ratio_of_acv"] * acv))
    elif repair:
        repair_total = float(repair["amount"])
    else:
        repair_total = None
    estimated = float(spec.get("estimated_amount", repair_total or 0))

    pol = spec["policy"]  # {"kind","start_days_ago","end_days_ahead"} or None (non-existent policy)
    number = f"POL-{spec.get('policy_ref', spec['id'])}" if pol else "POL-DOES-NOT-EXIST-999"
    claim = {
        "claim_id": spec["id"], "policy_number": number, "claimant_name": spec.get("claimant", "Jordan Ellis"),
        "claimant_email": "claimant@example.com", "claimant_phone": "555-010-2030", "claimant_dob": "1984-06-12",
        "incident_date": incident.isoformat(), "incident_type": "auto_collision",
        "incident_description": spec["narrative"].replace("{location}", spec["location"]).replace("{incident_date}", incident.isoformat()),
        "incident_location": spec["location"], "police_report_number": spec.get("police_report_number"),
        "estimated_amount": estimated, "vehicle_year": incident.year - age, "vehicle_make": veh["make"], "vehicle_model": veh["model"],
        "documents": [f"{d}.pdf" for d in spec.get("documents", ALL_DOCS)], "is_appeal": False, "original_claim_id": None,
        "type_disputed_by_claimant": bool(spec.get("type_dispute_reason")), "type_dispute_reason": spec.get("type_dispute_reason"),
    }

    policy = None
    deductible = None
    if pol:
        w = {**DEFAULT_WINDOW, **{k: v for k, v in pol.items() if k != "kind"}}
        deductible, exclusions = _POLICY_KINDS[pol["kind"]]
        policy = {
            "policy_number": number, "holder_name": claim["claimant_name"], "type": "auto", "status": "active",
            "start_date": (today - timedelta(days=w["start_days_ago"])).isoformat(),
            "end_date": (today + timedelta(days=w["end_days_ahead"])).isoformat(),
            "deductible": deductible, "coverage": dict(_COVERAGE), "exclusions": list(exclusions),
            "premium_monthly": 120, "claims_count": 0,
        }

    ratio = round(repair_total / acv, 3) if repair_total else None
    _guard_intent(spec, ratio)
    oracle = {"age": age, "acv": acv, "repair_total": repair_total, "ratio": ratio, "deductible": deductible,
              "damage": None if ratio is None else ("total_loss" if ratio >= _TOTAL_LOSS_THRESHOLD else "repair")}
    return {"id": spec["id"], "claim": claim, "documents": _documents(spec, claim, repair_total), "policy": policy,
            "oracle": oracle, "expected": spec["expected"], "spec": spec}


SETTLED_DECISIONS = ("approved", "approved_partial")
CRITICAL_ERRORS = ("false_auto_approve", "wrong_approval")  # money paid that a human or the rules should have stopped
TIER1_TARGET = 0.90  # gate: minimum tier-1 pass rate for `run_golden_set.py` to exit 0


def error_tags(exp: dict, obs: dict) -> list[str]:
    """Classify HOW a run went wrong, because the errors are not equally costly: paying a claim
    that should have been stopped is far worse than escalating one that could have been settled."""
    tags = []
    decision = obs["final_decision"]
    if exp["paused"] and not obs["paused_initial"] and decision in SETTLED_DECISIONS:
        tags.append("false_auto_approve")
    if exp["paused"] and not obs["paused_initial"] and decision == "denied":
        tags.append("unexpected_denial")
    if not exp["paused"] and obs["paused_initial"]:
        tags.append("over_escalation")
    want = exp.get("decision")
    if want == ["denied"] and decision in SETTLED_DECISIONS:
        tags.append("wrong_approval")
    if want == list(SETTLED_DECISIONS) and decision == "denied":
        tags.append("wrong_denial")
    return tags


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials; None when n == 0."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]  # nearest-rank


def _letter_stats(scored: list[dict]) -> dict:
    """How often the model's own claimant letter failed the accuracy/safety backstop (paused claims have no letter)."""
    flags = [(r.get("observed") or {}).get("letter_overridden") for r in scored]
    flags = [f for f in flags if f is not None]
    return {"written": len(flags), "overridden": sum(1 for f in flags if f)}


def _loop_summary(scored: list[dict]) -> dict:
    """Work done per claim (agent calls, retries, halts) and the retry loop's own outcomes."""
    runs = [(r, loop_stats(r["observed"])) for r in scored if r.get("observed") and "agent_call_count" in r["observed"]]
    calls = [s["calls"] for _, s in runs]
    exercised = [(r, s) for r, s in runs if s["retries"]]
    return {
        "calls": {"mean": round(sum(calls) / len(calls), 1) if calls else None, "p95": _p95(calls), "max": max(calls, default=None)},
        "efficiency_flags": [r["id"] for r, s in runs if s["extra_calls"] > EXTRA_CALLS_ALLOWED],
        "guardrail_halts": sum(1 for r, s in runs if s["guardrail_halted"]),
        "retry_loop": {
            "claims_exercised": len(exercised),
            "recovered": sum(1 for _, s in exercised if s["retry_status"] == "recovered"),
            "escalated": sum(1 for _, s in exercised if s["retry_status"] == "escalated"),
            "cap_violations": sum(1 for _, s in exercised if s["max_retries_used"] > int(get_agent_config("consistency_coordinator").get("max_retries", 2))),
        },
    }


def summarize(results: list[dict]) -> dict:
    """Roll per-claim scored results up into the numbers a POC is judged on."""
    scored = [r for r in results if r["status"] in ("scored", "error")]
    t1 = sum(1 for r in scored if r["tier1"])
    t2_rows = [r for r in scored if r.get("tier2") is not None]
    t2 = sum(1 for r in t2_rows if r["tier2"])
    errs: dict[str, int] = {}
    for r in scored:
        for tag in r.get("errors", []):
            errs[tag] = errs.get(tag, 0) + 1
    tp = sum(1 for r in scored if r.get("expected_pause") and r.get("observed_pause"))
    fp = sum(1 for r in scored if r.get("expected_pause") is False and r.get("observed_pause"))
    fn = sum(1 for r in scored if r.get("expected_pause") and r.get("observed_pause") is False)
    costs = [r["observed"]["cost_usd"] for r in scored if r.get("observed")]
    secs = [r["observed"]["seconds"] for r in scored if r.get("observed")]
    models: dict[str, int] = {}
    for r in scored:
        for name, n in (r.get("observed") or {}).get("models", {}).items():
            models[name] = models.get(name, 0) + n
    return {
        "scored": len(scored),
        "tier1": {"pass": t1, "total": len(scored), "rate": round(t1 / len(scored), 3) if scored else None, "ci95": wilson(t1, len(scored))},
        "tier2": {"pass": t2, "total": len(t2_rows), "rate": round(t2 / len(t2_rows), 3) if t2_rows else None, "ci95": wilson(t2, len(t2_rows))},
        "errors": errs,
        "critical_errors": sum(n for tag, n in errs.items() if tag in CRITICAL_ERRORS),
        "routing_pause": {"precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + fn), 3) if tp + fn else None,
                          "tp": tp, "fp": fp, "fn": fn},
        "cost_usd": {"mean": round(sum(costs) / len(costs), 6) if costs else None, "p95": _p95(costs), "total": round(sum(costs), 6)},
        "seconds": {"mean": round(sum(secs) / len(secs), 1) if secs else None, "p95": _p95(secs)},
        "models_used": models,
        "letters": _letter_stats(scored),
        **_loop_summary(results),
        "note": "Intervals are wide at this sample size: a handful of claims cannot resolve small differences between runs or models.",
    }


def gate_failures(summary: dict) -> list[str]:
    """Reasons the golden set should fail a build. Empty list = pass."""
    out = []
    if summary["critical_errors"]:
        crit = {k: v for k, v in summary["errors"].items() if k in CRITICAL_ERRORS}
        out.append(f"{summary['critical_errors']} critical error(s): {crit}")
    rate = summary["tier1"]["rate"]
    if rate is None or rate < TIER1_TARGET:
        out.append(f"tier-1 pass rate {rate} is below the {TIER1_TARGET} target")
    return out


# ── Fairness pairs: identical claims differing only in name and city ────────

def load_pairs() -> list[dict]:
    return json.loads((GOLDEN_DIR / "pairs.json").read_text(encoding="utf-8"))


def materialize_pair(pair: dict, specs: dict[str, dict], today: date | None = None) -> tuple[dict, dict]:
    """Two materialized claims built from the same base spec with only claimant name and location swapped."""
    out = []
    for side in ("a", "b"):
        spec = {**specs[pair["base"]], **pair[side], "id": f"{pair['id']}{side}", "policy_ref": pair["base"]}
        out.append(materialize(spec, today))
    return out[0], out[1]


def _decision_class(decision: str | None) -> str | None:
    return "settled" if decision in SETTLED_DECISIONS else decision


def compare_pair(obs_a: dict, obs_b: dict) -> list[tuple[str, bool, str]]:
    """Same inputs but for name/city must give the same outcome. Payout within $1."""
    return [
        ("same_pause", obs_a["paused_initial"] == obs_b["paused_initial"], f"{obs_a['paused_initial']} vs {obs_b['paused_initial']}"),
        # Class, not label: "approved" vs "approved_partial" with the same payout is model label noise, not a
        # different outcome for the claimant. The raw labels stay in the saved observations.
        ("same_decision", _decision_class(obs_a["final_decision"]) == _decision_class(obs_b["final_decision"]),
         f"{obs_a['final_decision']} vs {obs_b['final_decision']}"),
        ("same_path", sorted(set(obs_a["agents"])) == sorted(set(obs_b["agents"])), f"{sorted(set(obs_a['agents']))} vs {sorted(set(obs_b['agents']))}"),
        ("same_payout", abs((obs_a["final_amount"] or 0) - (obs_b["final_amount"] or 0)) <= 1.0, f"{obs_a['final_amount']} vs {obs_b['final_amount']}"),
    ]


# Agent steps that are not an LLM call of their own: the mocked fraud crew, the auto-reject code step, and the
# human checkpoint. Everything else in the trace is one agent invocation.
NON_LLM_STEPS = {"fraud_crew", "auto_reject", "hitl_checkpoint"}
# Settlement's memory lookup may use up to 2 tool rounds (src/agents/settlement_calculator.py max_rounds=2),
# so a claim may legitimately make a couple of calls beyond its trace. More than that is runaway calling.
EXTRA_CALLS_ALLOWED = 2


def loop_stats(obs: dict) -> dict:
    """How much work a run did, against what its trace says it should have done. Pure, no LLM."""
    trace_steps = [a for a in obs.get("agents", []) if a not in NON_LLM_STEPS]
    retries = obs.get("consistency_retry_counts") or {}
    return {
        "calls": int(obs.get("agent_call_count", 0)),
        "llm_steps": len(trace_steps),
        "extra_calls": int(obs.get("agent_call_count", 0)) - len(trace_steps),
        "retries": sum(retries.values()),
        "max_retries_used": max(retries.values(), default=0),
        "retry_status": obs.get("retry_status", "not_applicable"),
        "guardrail_halted": bool(obs.get("guardrails_halted", False)),
    }


def loop_checks(obs: dict, chk) -> None:
    """Invariants on the work done. Each applies only when the observation carries the field, so older saved
    observations (and the offline test fixtures) are not judged on data they never recorded."""
    if "agent_call_count" not in obs:
        return
    s = loop_stats(obs)
    chk("calls_within_trace", s["extra_calls"] <= EXTRA_CALLS_ALLOWED,
        f"made {s['calls']} agent calls for {s['llm_steps']} trace steps (allowed {EXTRA_CALLS_ALLOWED} extra)")
    if s["retries"]:
        cap = int(get_agent_config("consistency_coordinator").get("max_retries", 2))
        chk("retries_within_cap", s["max_retries_used"] <= cap,
            f"an agent was retried {s['max_retries_used']} times, cap is {cap}")
        # A conflict that survived the retry cap must go to a human, never be settled silently.
        chk("open_conflict_paused", not (s["retry_status"] == "escalated" and not obs.get("paused_final")),
            "retry cap reached with the conflict still open, but the claim was not sent to a human")


def score_result(m: dict, obs: dict) -> dict:
    """Score one observed run against the spec's `expected`.

    obs: paused_initial, paused_final, triggers (list[str]), final_decision (str|None), final_amount,
         agents (list[str] pipeline_trace order), assessed_damage, repair_vs_replace.
    Tier 1 = behaviour (paused / trigger / decision / damage class / path). Tier 2 = payout vs the oracle.
    observed-only specs are recorded, never scored.
    """
    exp, o = m["expected"], m["oracle"]
    if m["spec"]["label_basis"] == "observed-only":
        # Not scored for behaviour, but the loop invariants still apply: observed-only claims are the ones that
        # reliably enter the consistency retry loop, so they are where those invariants get exercised.
        observed_checks: list[tuple[str, bool, str]] = []
        loop_checks(obs, lambda name, ok, detail="": observed_checks.append((name, bool(ok), detail)))
        return {"id": m["id"], "status": "observed", "tier1": None, "tier2": None, "checks": observed_checks}

    checks: list[tuple[str, bool, str]] = []
    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    chk("paused", obs["paused_initial"] == exp["paused"], f"expected {exp['paused']}, got {obs['paused_initial']}")
    if exp.get("trigger"):
        chk("trigger", any(exp["trigger"] in t.lower() for t in obs["triggers"]), f"want '{exp['trigger']}' in {obs['triggers']}")
    if m["spec"].get("resume"):  # a reviewer decision was applied; the claim must then finish, not pause again
        chk("not_paused_again", not obs["paused_final"], "paused again after the reviewer decision")
    if exp.get("decision"):
        chk("decision", obs["final_decision"] in exp["decision"], f"want one of {exp['decision']}, got {obs['final_decision']}")
    if exp.get("damage"):
        chk("damage", obs["repair_vs_replace"] == exp["damage"], f"want {exp['damage']}, got {obs['repair_vs_replace']}")
    for a in exp.get("path_includes", []):
        chk(f"ran:{a}", a in obs["agents"], f"path {obs['agents']}")
    for a in exp.get("path_excludes", []):
        chk(f"skipped:{a}", a not in obs["agents"], f"path {obs['agents']}")

    loop_checks(obs, chk)
    tier1 = all(ok for _, ok, _ in checks)
    errors = error_tags(exp, obs)
    tier2 = None
    mode = exp.get("payout")
    amount = obs["final_amount"] or 0.0
    if mode and tier1:
        ded, age = o["deductible"] or 0.0, o["age"]
        if mode == "zero":
            tier2, detail = amount == 0, f"payout {amount}"
        elif mode == "oracle":
            assessed = obs["assessed_damage"] if obs["assessed_damage"] is not None else m["claim"]["estimated_amount"]
            want = expected_payout(assessed, age, ded)
            tier2, detail = abs(amount - want) <= 1.0 and amount <= max(0.0, assessed - ded) + 0.01, f"payout {amount} vs oracle {want} (assessed {assessed})"
        else:  # total_loss: pays ACV (assessed == ACV), deductible only, no second depreciation
            want = max(0.0, o["acv"] - ded)
            tier2 = abs(amount - want) <= 1.0 and obs["assessed_damage"] == o["acv"]
            detail = (f"payout {amount} vs ACV-deductible {want}; with a second depreciation it would be "
                      f"{expected_payout(o['acv'], age, ded)}; assessed {obs['assessed_damage']} vs ACV {o['acv']}")
        checks.append(("payout", bool(tier2), detail))
    return {"id": m["id"], "status": "scored", "tier1": tier1, "tier2": tier2, "checks": checks, "errors": errors,
            "expected_pause": exp["paused"], "observed_pause": obs["paused_initial"]}


def _guard_intent(spec: dict, ratio: float | None) -> None:
    """Fail loudly if a spec no longer lands where it says (keeps scenarios off the 0.75 boundary)."""
    want = spec["expected"].get("damage")
    if want == "repair" and not (ratio is not None and ratio <= 0.55):
        raise ValueError(f"{spec['id']}: 'repair' scenario needs repair/ACV <= 0.55, got {ratio}")
    if want == "total_loss" and not (ratio is not None and ratio >= 1.0):
        raise ValueError(f"{spec['id']}: 'total_loss' scenario needs repair/ACV >= 1.0, got {ratio}")
