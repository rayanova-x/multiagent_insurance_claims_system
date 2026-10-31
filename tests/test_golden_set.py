"""Offline checks for the demo golden set (data/golden_set/, src/data/golden.py).

No LLM calls. Proves: the 20 specs are well-formed, they materialize to the
SAME scenario whatever the calendar year (the time-proofing that motivated the
relative-spec design), the independent payout oracle agrees with production's
ACV/depreciation/policy functions, and the scoring function behaves.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal

import pytest

from src.data.golden import (
    HIGH_VALUE_THRESHOLD, acv_for_age, depreciation_for, expected_payout, load_specs, materialize, score_result,
)
from src.tools.damage_calculator import apply_depreciation_country_aware, calculate_vehicle_acv
from src.tools.policy_lookup import is_policy_active
from src.utils import calculate_asset_age

SPECS = load_specs()
BY_ID = {s["id"]: s for s in SPECS}
TODAYS = [date(2026, 10, 30), date(2028, 3, 1), date(2031, 7, 15)]


def test_twenty_specs_with_unique_sequential_ids():
    assert [s["id"] for s in SPECS] == [f"GOLD-{n:02d}" for n in range(1, 21)]
    assert {s["label_basis"] for s in SPECS} <= {"code-deterministic", "rule-text", "observed-only"}
    assert all(s["narrative"].strip() and s["rule_citation"].strip() for s in SPECS)


def test_exactly_one_observed_only_and_no_fraud_scenario():
    assert [s["id"] for s in SPECS if s["label_basis"] == "observed-only"] == ["GOLD-20"]
    assert not any("fraud" in s["slug"] for s in SPECS)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s["id"])
def test_scenario_is_identical_across_calendar_years(spec):
    ref = materialize(spec, TODAYS[0])
    for today in TODAYS[1:]:
        m = materialize(spec, today)
        assert m["oracle"] == ref["oracle"], "age-derived values must not drift with the calendar"
        assert m["claim"]["estimated_amount"] == ref["claim"]["estimated_amount"]
        assert m["claim"]["vehicle_year"] == date.fromisoformat(m["claim"]["incident_date"]).year - spec["vehicle"]["age_years"]
        if m["policy"]:
            active, _ = is_policy_active(m["policy"], m["claim"]["incident_date"])
            ref_active, _ = is_policy_active(ref["policy"], ref["claim"]["incident_date"])
            assert active == ref_active == (spec["id"] != "GOLD-06")


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s["id"])
def test_oracle_agrees_with_production_functions(spec):
    m = materialize(spec)
    c, o = m["claim"], m["oracle"]
    assert calculate_vehicle_acv(c["vehicle_year"], c["vehicle_make"], c["vehicle_model"], as_of=c["incident_date"]) == o["acv"]
    assert calculate_asset_age(c["vehicle_year"], c["incident_date"]) == o["age"]
    for damage in (400.0, 5850.0, 11200.0):
        assert apply_depreciation_country_aware(damage, "auto", o["age"])[1] == pytest.approx(depreciation_for(damage, o["age"]), abs=0.01)


def test_acv_and_payout_hand_values():
    assert acv_for_age(2) == 28000 and acv_for_age(8) == 13000
    # GOLD-01-style: 11,200 repair, age 2 (15%/yr): dep = 11200*0.15*2 = 3360; 11200-3360-500
    assert expected_payout(11200, 2, 500) == 7340.0
    assert expected_payout(400, 6, 100) == 132.0  # 400 - 168 - 100
    assert expected_payout(10000, 4, 15000) == 0.0  # deductible exceeds damage
    assert depreciation_for(1000, 20) == 700.0  # 70% cap


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s["id"])
def test_documents_match_claim_checklist_and_sum_exactly(spec):
    m = materialize(spec)
    docs = m["documents"] or {}
    names = {n.removesuffix(".pdf") for n in m["claim"]["documents"]}
    assert set(docs) <= names
    if spec.get("repair") and "repair_estimate" in names:
        re_ = docs["repair_estimate"]
        assert sum(Decimal(i["amount"]) for i in re_["line_items"]) == Decimal(re_["line_items_sum"]) == Decimal(re_["stated_total"])
    if spec["id"] == "GOLD-13":
        assert "repair_estimate" not in docs  # the whole point of that scenario


def test_amount_bands_match_each_scenarios_intent():
    ms = {s["id"]: materialize(s) for s in SPECS}
    assert ms["GOLD-05"]["claim"]["estimated_amount"] < 500  # fast-mode cutoff
    for gid in ("GOLD-15", "GOLD-19"):
        assert ms[gid]["claim"]["estimated_amount"] >= HIGH_VALUE_THRESHOLD
    for gid, m in ms.items():
        if gid not in ("GOLD-15", "GOLD-19"):
            assert m["claim"]["estimated_amount"] < HIGH_VALUE_THRESHOLD, gid
    assert ms["GOLD-07"]["policy"] is None and ms["GOLD-07"]["claim"]["policy_number"].startswith("POL-DOES-NOT-EXIST")
    assert ms["GOLD-17"]["claim"]["type_disputed_by_claimant"] and ms["GOLD-18"]["claim"]["type_disputed_by_claimant"]
    assert ms["GOLD-04"]["oracle"]["damage"] == "total_loss"
    assert all(ms[g]["oracle"]["damage"] == "repair" for g in ("GOLD-01", "GOLD-02", "GOLD-03", "GOLD-09", "GOLD-11"))


def test_intent_guard_fails_loudly_when_a_scenario_drifts_to_the_boundary():
    bad = copy.deepcopy(BY_ID["GOLD-01"])
    bad["repair"]["ratio_of_acv"] = 0.74  # would sit next to the 0.75 total-loss line
    with pytest.raises(ValueError, match="needs repair/ACV <= 0.55"):
        materialize(bad)
    bad = copy.deepcopy(BY_ID["GOLD-04"])
    bad["repair"]["ratio_of_acv"] = 0.80
    with pytest.raises(ValueError, match="needs repair/ACV >= 1.0"):
        materialize(bad)


def _obs(**kw):
    base = dict(paused_initial=False, paused_final=False, triggers=[], final_decision="approved", final_amount=0.0,
                agents=["intake_agent", "damage_assessor", "settlement_calculator", "communication_agent"],
                assessed_damage=None, repair_vs_replace="repair")
    base.update(kw)
    return base


def _checks(result: dict) -> dict:
    return {n: ok for n, ok, _ in result["checks"]}


def test_loop_checks_are_not_applied_to_observations_that_never_recorded_them():
    # Saved runs from before this change have no agent_call_count: they must not be judged on it.
    r = score_result(materialize(BY_ID["GOLD-01"]), _obs(assessed_damage=11200.0, final_amount=7340.0))
    assert "calls_within_trace" not in _checks(r)


def test_extra_agent_calls_beyond_the_allowance_fail_the_efficiency_check():
    m = materialize(BY_ID["GOLD-01"])
    ok = score_result(m, _obs(assessed_damage=11200.0, final_amount=7340.0, agent_call_count=6, retry_status="not_applicable"))
    assert _checks(ok)["calls_within_trace"] is True  # 4 trace steps + 2 allowed
    runaway = score_result(m, _obs(assessed_damage=11200.0, final_amount=7340.0, agent_call_count=7, retry_status="not_applicable"))
    assert _checks(runaway)["calls_within_trace"] is False


def test_fraud_crew_and_checkpoint_steps_do_not_count_as_llm_steps():
    from src.data.golden import loop_stats
    s = loop_stats(_obs(agents=["intake_agent", "fraud_crew", "hitl_checkpoint", "communication_agent"], agent_call_count=2))
    assert s["llm_steps"] == 2 and s["extra_calls"] == 0


def test_a_retry_past_the_configured_cap_fails_and_one_within_it_passes():
    from src.config import get_agent_config
    cap = int(get_agent_config("consistency_coordinator").get("max_retries", 2))
    m = materialize(BY_ID["GOLD-01"])
    within = score_result(m, _obs(assessed_damage=11200.0, final_amount=7340.0, agent_call_count=4,
                                  consistency_retry_counts={"damage_assessor": cap}, retry_status="recovered"))
    assert _checks(within)["retries_within_cap"] is True
    over = score_result(m, _obs(assessed_damage=11200.0, final_amount=7340.0, agent_call_count=4,
                                consistency_retry_counts={"damage_assessor": cap + 1}, retry_status="recovered"))
    assert _checks(over)["retries_within_cap"] is False


def test_a_conflict_that_survives_the_cap_must_end_paused_not_settled():
    m = materialize(BY_ID["GOLD-01"])
    base = dict(assessed_damage=11200.0, final_amount=7340.0, agent_call_count=4,
                consistency_retry_counts={"damage_assessor": 1}, retry_status="escalated")
    settled = score_result(m, _obs(**base, paused_final=False))
    assert _checks(settled)["open_conflict_paused"] is False
    paused = score_result(m, _obs(**base, paused_final=True))
    assert _checks(paused)["open_conflict_paused"] is True


def test_retry_status_comes_from_the_trajectory_module_not_a_second_copy():
    # Same rule the trajectory analysis uses: retries ran and has_conflicts is False afterwards = recovered.
    from types import SimpleNamespace
    from evaluation.trajectory import _retry_recovery_status
    state = {"consistency_retry_counts": {"policy_checker": 1}, "consistency_output": SimpleNamespace(has_conflicts=False)}
    assert _retry_recovery_status(state) == "recovered"


def test_summary_counts_calls_halts_flags_and_the_retry_loop():
    counter = iter(range(100))

    def row(calls, halted=False, retries=None, status="not_applicable"):
        return {"id": f"T{next(counter)}", "status": "scored", "tier1": True, "tier2": None, "errors": [], "expected_pause": False,
                "observed_pause": False, "observed": {"cost_usd": 0.01, "seconds": 10.0, "models": {"m": 1},
                "agent_call_count": calls, "guardrails_halted": halted, "consistency_retry_counts": retries or {},
                "retry_status": status, "agents": ["intake_agent", "damage_assessor", "settlement_calculator",
                                                  "communication_agent"]}}
    rows = [row(4), row(4, halted=True), row(9), row(4, retries={"damage_assessor": 1}, status="recovered"),
            row(4, retries={"policy_checker": 3}, status="escalated")]
    s = summarize(rows)
    assert s["guardrail_halts"] == 1
    assert s["efficiency_flags"] == ["T2"]  # the 9-call claim: 5 extra against an allowance of 2
    assert s["calls"]["max"] == 9
    assert s["retry_loop"] == {"claims_exercised": 2, "recovered": 1, "escalated": 1, "cap_violations": 1}


def test_score_result_oracle_total_loss_and_observed_only():
    m1 = materialize(BY_ID["GOLD-01"])
    good = score_result(m1, _obs(assessed_damage=11200.0, final_amount=7340.0))
    assert good["tier1"] and good["tier2"]
    bad_amt = score_result(m1, _obs(assessed_damage=11200.0, final_amount=9000.0))
    assert bad_amt["tier1"] and bad_amt["tier2"] is False
    wrong_path = score_result(m1, _obs(assessed_damage=11200.0, final_amount=7340.0, paused_initial=True))
    assert wrong_path["tier1"] is False and wrong_path["tier2"] is None

    m4 = materialize(BY_ID["GOLD-04"])  # ACV 13,000, deductible 500 -> 12,500
    tl = score_result(m4, _obs(repair_vs_replace="total_loss", assessed_damage=13000.0, final_amount=12500.0))
    assert tl["tier1"] and tl["tier2"]
    double_dep = score_result(m4, _obs(repair_vs_replace="total_loss", assessed_damage=13000.0, final_amount=6130.0))
    assert double_dep["tier2"] is False and "second depreciation" in dict((n, d) for n, _, d in double_dep["checks"])["payout"]

    m20 = materialize(BY_ID["GOLD-20"])
    assert score_result(m20, _obs())["status"] == "observed"


def test_score_result_pause_trigger_and_resume():
    m = materialize(BY_ID["GOLD-19"])
    ok = score_result(m, _obs(paused_initial=True, triggers=["Claim amount $91,000 exceeds review threshold $84,100"],
                              final_decision="denied", final_amount=0.0, agents=["intake_agent", "communication_agent"]))
    assert ok["tier1"] and ok["tier2"]
    ran_settlement = score_result(m, _obs(paused_initial=True, triggers=["Claim amount $91,000 exceeds review threshold $84,100"],
                                          final_decision="denied", agents=["intake_agent", "settlement_calculator"]))
    assert ran_settlement["tier1"] is False


# ── Error classification, summary, gate, intervals, pairs ──────────────────

from src.data.golden import (  # noqa: E402
    TIER1_TARGET, compare_pair, error_tags, gate_failures, load_pairs, materialize_pair, summarize, wilson,
)


def test_error_tags_separate_dangerous_from_merely_cautious_mistakes():
    pause, settle, deny = {"paused": True}, {"paused": False, "decision": ["approved", "approved_partial"]}, {"paused": False, "decision": ["denied"]}
    o = lambda **kw: {**dict(paused_initial=False, final_decision="approved"), **kw}  # noqa: E731
    assert error_tags(pause, o()) == ["false_auto_approve"]  # should have stopped, paid
    assert error_tags(pause, o(final_decision="denied")) == ["unexpected_denial"]
    assert error_tags(pause, o(paused_initial=True)) == []
    assert error_tags(settle, o(paused_initial=True, final_decision=None)) == ["over_escalation"]
    assert error_tags(deny, o()) == ["wrong_approval"]
    assert error_tags(settle, o(final_decision="denied")) == ["wrong_denial"]
    assert error_tags(settle, o()) == []
    # a reviewer-denied claim that is then approved anyway is a critical error too
    assert error_tags({"paused": True, "decision": ["denied"]}, o(paused_initial=True)) == ["wrong_approval"]


def test_wilson_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(19, 20)
    assert 0.74 < lo < 0.78 and 0.97 < hi <= 1.0
    assert wilson(20, 20)[1] == 1.0 and wilson(0, 20)[0] == 0.0
    assert wilson(5, 10)[0] < 0.5 < wilson(5, 10)[1]


def _row(tier1=True, tier2=None, errors=(), exp_pause=False, obs_pause=False, cost=0.01, secs=10.0, models=None):
    return {"status": "scored", "tier1": tier1, "tier2": tier2, "errors": list(errors), "expected_pause": exp_pause,
            "observed_pause": obs_pause, "observed": {"cost_usd": cost, "seconds": secs, "models": models or {"m": 2}}}


def test_summarize_and_gate():
    rows = [_row(tier2=True), _row(tier2=False, tier1=True), _row(exp_pause=True, obs_pause=True),
            _row(exp_pause=True, obs_pause=False, tier1=False, errors=["false_auto_approve"]),
            _row(exp_pause=False, obs_pause=True, errors=["over_escalation"], cost=0.05, secs=40.0)]
    s = summarize(rows + [{"status": "observed"}])
    assert s["scored"] == 5 and s["tier1"]["pass"] == 4
    assert s["tier2"] == {"pass": 1, "total": 2, "rate": 0.5, "ci95": wilson(1, 2)}
    assert s["critical_errors"] == 1 and s["errors"] == {"false_auto_approve": 1, "over_escalation": 1}
    assert s["routing_pause"] == {"precision": 0.5, "recall": 0.5, "tp": 1, "fp": 1, "fn": 1}
    assert s["cost_usd"]["p95"] == 0.05 and s["seconds"]["p95"] == 40.0 and s["models_used"] == {"m": 10}
    reasons = gate_failures(s)
    assert any("critical" in r for r in reasons) and any("tier-1" in r for r in reasons)
    good = summarize([_row() for _ in range(10)])
    assert gate_failures(good) == [] and good["tier1"]["rate"] >= TIER1_TARGET


def test_fairness_pairs_differ_only_in_name_and_city():
    specs = {s["id"]: s for s in SPECS}
    pairs = load_pairs()
    assert len(pairs) == 3 and all(p["base"] in specs for p in pairs)
    for p in pairs:
        a, b = materialize_pair(p, specs)
        assert a["claim"]["claimant_name"] != b["claim"]["claimant_name"]
        assert a["claim"]["incident_location"] != b["claim"]["incident_location"]
        assert a["oracle"] == b["oracle"] and a["claim"]["policy_number"] == b["claim"]["policy_number"] == f"POL-{p['base']}"
        assert a["claim"]["estimated_amount"] == b["claim"]["estimated_amount"]
        assert a["expected"] == b["expected"] == specs[p["base"]]["expected"]


def test_compare_pair_flags_a_difference_in_outcome():
    base = dict(paused_initial=False, final_decision="approved", agents=["a", "b"], final_amount=5000.0)
    assert all(ok for _, ok, _ in compare_pair(base, dict(base, final_amount=5000.5)))
    assert not dict((n, ok) for n, ok, _ in compare_pair(base, dict(base, final_amount=4000.0)))["same_payout"]
    assert not dict((n, ok) for n, ok, _ in compare_pair(base, dict(base, final_decision="denied")))["same_decision"]
    assert not dict((n, ok) for n, ok, _ in compare_pair(base, dict(base, paused_initial=True)))["same_pause"]


# ── Crash-safe persistence: every finished claim is on disk immediately ────

def test_partial_results_survive_a_crash_and_a_torn_last_line(tmp_path):
    from scripts.run_golden_set import _append_partial, _load_partial

    path = tmp_path / "x.partial.jsonl"
    assert _load_partial(path) == {}
    _append_partial(path, {"kind": "claim", "id": "GOLD-01", "status": "scored", "tier1": True})
    _append_partial(path, {"kind": "pair", "id": "PAIR-01", "ok": True})
    with path.open("a", encoding="utf-8") as f:
        f.write('{"kind": "claim", "id": "GOLD-02", "sta')  # process died mid-write
    done = _load_partial(path)
    assert set(done) == {("claim", "GOLD-01"), ("pair", "PAIR-01")}
    assert done[("claim", "GOLD-01")]["tier1"] is True


def test_pair_compares_decision_class_not_the_approved_vs_partial_label():
    base = dict(paused_initial=False, final_decision="approved", agents=["a"], final_amount=3900.0)
    same_money = dict(base, final_decision="approved_partial")  # live run: identical payout, different label
    assert all(ok for _, ok, _ in compare_pair(base, same_money))
    assert not dict((n, ok) for n, ok, _ in compare_pair(base, dict(base, final_decision="denied", final_amount=0.0)))["same_decision"]


def test_summary_counts_letters_the_backstop_had_to_replace():
    rows = [_row(), _row(), _row()]
    rows[0]["observed"]["letter_overridden"] = False
    rows[1]["observed"]["letter_overridden"] = True
    rows[2]["observed"]["letter_overridden"] = None  # paused, no letter
    assert summarize(rows)["letters"] == {"written": 2, "overridden": 1}


def test_a_claim_that_pauses_again_after_the_reviewer_decision_fails_tier_one():
    # GOLD-18 live: reviewer approved, then the evaluator gate paused it again; the old check never ran
    # because it read `resume` from the wrong dict.
    m = materialize(BY_ID["GOLD-18"])
    again = score_result(m, _obs(paused_initial=True, paused_final=True, triggers=["Claimant disputed suggested incident_type"],
                                 assessed_damage=8000.0, final_amount=2000.0,
                                 agents=["damage_assessor", "settlement_calculator", "communication_agent"]))
    assert dict((n, ok) for n, ok, _ in again["checks"])["not_paused_again"] is False
    done = score_result(m, _obs(paused_initial=True, paused_final=False, triggers=["Claimant disputed suggested incident_type"],
                                assessed_damage=8000.0, final_amount=2000.0,
                                agents=["damage_assessor", "settlement_calculator", "communication_agent"]))
    assert dict((n, ok) for n, ok, _ in done["checks"])["not_paused_again"] is True


def test_a_swallowed_pipeline_crash_is_an_error_not_a_scored_result():
    import pytest
    from scripts.run_golden_set import _raise_on_crash

    _raise_on_crash({"state": {"error_log": []}})
    _raise_on_crash({"state": {}})
    with pytest.raises(RuntimeError, match="Pipeline crash: boom"):
        _raise_on_crash({"state": {"error_log": ["Pipeline crash: boom"]}})


def test_observed_only_claims_still_have_their_loop_invariants_checked_and_counted():
    # GOLD-20 live: it entered the retry loop and recovered, but observed-only claims were never checked or counted.
    m = materialize(BY_ID["GOLD-20"])
    r = score_result(m, _obs(agent_call_count=4, consistency_retry_counts={"damage_assessor": 1}, retry_status="recovered"))
    assert r["status"] == "observed" and r["tier1"] is None
    assert _checks(r)["calls_within_trace"] is True and _checks(r)["retries_within_cap"] is True
    results = [{"id": "GOLD-20", "status": "observed", "tier1": None, "tier2": None, "checks": r["checks"],
                "observed": {"agent_call_count": 4, "consistency_retry_counts": {"damage_assessor": 1},
                             "retry_status": "recovered", "agents": ["intake_agent"], "guardrails_halted": False}}]
    assert summarize(results)["retry_loop"] == {"claims_exercised": 1, "recovered": 1, "escalated": 0, "cap_violations": 0}
