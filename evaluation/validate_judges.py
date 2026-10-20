"""Judge validation harness - runs every case in evaluation/judge_golden_set.py
through the REAL LLM judge it targets (real Gemini calls, no mocking) and
reports agreement against the golden set's proxy labels.

Per the baswe.ai evaluation field guide ss3.5 ("validate the judge, non
negotiable"): a judge is itself a system that must be evaluated before its
scores are trusted. See judge_golden_set.py's module docstring for the
honest limitation here - these are Claude-proxy labels, not human-adjuster
ground truth (no honest source of real human labels exists for fairness/
transparency, since those are judgments about this pipeline's own output).
consistency_coordinator's cases draw on real fraud_reported ground truth
where noted, but the conflict scenarios themselves are constructed.

Usage:
    python evaluation/validate_judges.py              # all judges
    python evaluation/validate_judges.py --judge fairness
    python evaluation/validate_judges.py --judge consistency_coordinator

Needs a real LLM_PROVIDER configured (.env) - this makes real API calls and
consumes real quota. Each case is one LLM call, so the full golden set
(14 cases) costs 14 calls, not the full-pipeline multi-call cost of a real
claim run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_core.messages import HumanMessage, SystemMessage

from evaluation.judge_golden_set import GOLDEN_SET
from src.agents.consistency_coordinator import SYSTEM_PROMPT as CC_SYSTEM_PROMPT
from src.evaluation.evaluator import FAIRNESS_SYSTEM_PROMPT, TRANSPARENCY_SYSTEM_PROMPT
from src.llm import get_judge_llm, get_structured_llm
from src.models.schemas import ConsistencyCheckOutput, JudgeDimensionOutput
from src.utils import currency_symbol as _sym


def _run_consistency_coordinator_case(case: dict) -> dict:
    """Builds the exact same prompt shape run_consistency_coordinator uses
    (src/agents/consistency_coordinator.py), from the case's constructed
    damage/policy/fraud inputs, and calls the real judge directly - no
    graph, no mocking, same system prompt/schema as production."""
    inp = case["inputs"]
    d, p, f = inp["damage_output"], inp["policy_output"], inp["fraud_output"]
    _s = _sym()

    damage_summary = f"""
    Conclusion: {d['repair_vs_replace']}
    Assessed Amount: {_s}{d['assessed_damage_usd']:,.2f}
    Notes: {d['assessment_notes']}
"""
    policy_summary = f"""
    Coverage Status: {p['coverage_status']}
    Exclusions Triggered: {', '.join(p['exclusions_triggered']) or 'None'}
    Notes: {p['coverage_notes']}
"""
    fraud_summary = f"""
    Fraud Risk: {f['fraud_risk_level']} (score {f['fraud_score']:.2f})
    Narrative/Story Summary: {f['crew_summary']}
"""
    prompt = f"""
    Review these three independent, isolated specialist findings for this claim:

    === DAMAGE ASSESSOR'S FINDINGS ===
    {damage_summary}

    === POLICY CHECKER'S FINDINGS ===
    {policy_summary}

    === FRAUD CREW'S FINDINGS ===
    {fraud_summary}

    Check ONLY for MISSED_FACT (damage surfaced something policy never saw)
    and SEVERITY_MISMATCH (damage's conclusion contradicts fraud's
    narrative-severity read), per the criteria in your instructions.
    """
    llm = get_structured_llm(ConsistencyCheckOutput)
    output = llm.invoke([
        SystemMessage(content=CC_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])
    conflicts = [
        {"type": c.conflict_type.value, "severity": c.severity.value, "description": c.description}
        for c in output.conflicts
    ]
    return {
        "has_conflicts": output.has_conflicts,
        "conflict_type": conflicts[0]["type"] if conflicts else None,
        "severity": conflicts[0]["severity"] if conflicts else None,
        "raw_summary": output.summary,
        "raw_conflicts": conflicts,
    }


def _run_fairness_or_transparency_case(case: dict) -> dict:
    """Same prompt shape as _judge_dimension in src/evaluation/evaluator.py."""
    inp = case["inputs"]
    system_prompt = FAIRNESS_SYSTEM_PROMPT if case["judge"] == "fairness" else TRANSPARENCY_SYSTEM_PROMPT

    if case["judge"] == "fairness":
        user_prompt = (
            f"Claim: {inp['claim_summary']}\n\n"
            f"Settlement decision: {inp['settlement_decision']}\n\n"
            f"Calculation breakdown:\n" + "\n".join(f"- {s}" for s in inp["calculation_breakdown"]) +
            "\n\nWould a human adjuster reach the same conclusion on this claim?"
        )
    else:
        user_prompt = (
            f"Calculation breakdown:\n" + "\n".join(f"- {s}" for s in inp["calculation_breakdown"]) +
            (f"\n\nDenial reasons:\n" + "\n".join(f"- {s}" for s in inp["denial_reasons"]) if inp.get("denial_reasons") else "") +
            "\n\nIs the reasoning above explainable and traceable, step by step?"
        )

    llm = get_judge_llm().with_structured_output(JudgeDimensionOutput)
    output = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ])
    return {"score": output.score, "raw_reasoning": output.reasoning}


def _grade_consistency(actual: dict, expected: dict) -> bool:
    """A consistency-coordinator case is correct if has_conflicts matches,
    AND (when a conflict is expected) the conflict_type matches too -
    severity is reported but not graded strictly (MATERIAL vs NOTABLE is a
    closer judgment call than conflict existence/type, and the golden set's
    own reasoning already explains why each severity was assigned, for a
    human reviewer to weigh rather than hard-fail on)."""
    if actual["has_conflicts"] != expected["has_conflicts"]:
        return False
    if expected["has_conflicts"] and actual["conflict_type"] != expected["conflict_type"]:
        return False
    return True


def _grade_score(actual_score, expected_score, tolerance: float = 0.3) -> bool:
    """A fairness/transparency case is correct if the judge's score lands
    within `tolerance` of the proxy label - these are continuous judgments,
    not categorical, so exact match is the wrong bar. A None score (judge
    couldn't decide) is graded as WRONG against any non-null expected
    label - the golden set never expects "unknown" for these clear-cut
    cases, so a null here is itself a miscalibration to surface."""
    if actual_score is None:
        return False
    return abs(actual_score - expected_score) <= tolerance


def run_validation(judge_filter: str | None = None) -> dict:
    cases = [c for c in GOLDEN_SET if judge_filter is None or c["judge"] == judge_filter]
    if not cases:
        print(f"No cases found for judge={judge_filter!r}")
        return {}

    results = []
    for case in cases:
        print(f"Running {case['case_id']} ({case['judge']})...")
        try:
            if case["judge"] == "consistency_coordinator":
                actual = _run_consistency_coordinator_case(case)
                correct = _grade_consistency(actual, case["proxy_label"])
            else:
                actual = _run_fairness_or_transparency_case(case)
                correct = _grade_score(actual["score"], case["proxy_label"]["score"])
            results.append({
                "case_id": case["case_id"], "judge": case["judge"],
                "correct": correct, "expected": case["proxy_label"], "actual": actual,
            })
        except Exception as e:
            results.append({
                "case_id": case["case_id"], "judge": case["judge"],
                "correct": None, "error": f"{type(e).__name__}: {e}",
            })

    by_judge: dict[str, list[dict]] = {}
    for r in results:
        by_judge.setdefault(r["judge"], []).append(r)

    print("\n" + "=" * 60)
    print("JUDGE VALIDATION RESULTS")
    print("=" * 60)
    summary = {}
    for judge_name, judge_results in by_judge.items():
        graded = [r for r in judge_results if r["correct"] is not None]
        agree = sum(1 for r in graded if r["correct"])
        errored = [r for r in judge_results if r["correct"] is None]
        rate = agree / len(graded) if graded else None
        summary[judge_name] = {
            "total_cases": len(judge_results), "graded": len(graded),
            "agreement": agree, "agreement_rate": rate, "errors": len(errored),
        }
        print(f"\n{judge_name}: {agree}/{len(graded)} agree with proxy labels "
              f"({rate:.0%})" if rate is not None else f"\n{judge_name}: no graded cases")
        for r in judge_results:
            if r["correct"] is None:
                print(f"  [ERROR] {r['case_id']}: {r.get('error')}")
            else:
                mark = "AGREE" if r["correct"] else "DISAGREE"
                print(f"  [{mark}] {r['case_id']}: expected={r['expected']} actual={r['actual']}")

    print("\n" + "=" * 60)
    print("Reminder: these are Claude-proxy labels, NOT human-adjuster-")
    print("validated ground truth. See judge_golden_set.py's docstring.")
    print("=" * 60)

    return {"summary": summary, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", choices=["consistency_coordinator", "fairness", "transparency"], default=None)
    parser.add_argument("--json-out", default=None, help="Write full results as JSON to this path")
    args = parser.parse_args()

    output = run_validation(args.judge)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
        print(f"\nFull results written to {args.json_out}")
