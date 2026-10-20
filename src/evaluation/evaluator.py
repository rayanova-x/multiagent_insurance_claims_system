"""
Claim-decision quality evaluator.

Grades the completed settlement decision before it's released. Two
dimensions, each an isolated LLM-as-judge call, plus one code-verified fact:

  1. accuracy_verified (CODE, not an LLM call) - does the settlement amount
     respect its own invariants (never exceeds damage minus deductible,
     never exceeds the coverage limit)? This is pure arithmetic on numbers
     already in settlement_output - asking an LLM to "check the math" by
     reading a text description of it cannot actually verify anything code
     doesn't already guarantee (see CLAUDE.md "schema validity is not
     correctness").
  2. fairness (LLM judge, isolated call) - would a human adjuster reach the
     same conclusion on this specific claim?
  3. transparency (LLM judge, isolated call) - is the reasoning explainable
     and traceable?

The original design had 5 LLM-graded dimensions in one combined call
(accuracy, completeness, fairness, safety, transparency). Investigation
found accuracy/safety/completeness were mostly redundant with facts code
already guarantees upstream (settlement's own hard caps, the fraud-score
code-enforced denial, policy_checker's code-list-driven exclusion check) -
asking an LLM to re-grade them from text wasn't independent verification,
just restating what upstream agents' own hard rules already produced.
Per CLAUDE.md's "multi-agent needs one of three specific justifications"
(context isolation, real parallelism, or independent verification with a
fresh context), only fairness and transparency are genuine second-pass
judgment calls that don't reduce to a formula. Per Anthropic's "Demystifying
evals for AI agents": grade each dimension with an ISOLATED judge call, not
one call scoring several dimensions at once, and give the judge a real
"Unknown" escape hatch rather than forcing a guess.

If overall_score < min_score_to_release, the pipeline re-routes for human
review rather than auto-releasing the decision.
"""

from __future__ import annotations

import logging
import time

from langchain_core.messages import HumanMessage, SystemMessage

from src.config import get_evaluation_config
from src.llm import get_judge_llm
from src.models.schemas import EvaluationOutput, JudgeDimensionOutput
from src.models.state import ClaimsState
from src.security.audit_log import log_agent_action
from src.security.pii_masker import mask_claim
from src.utils import currency_symbol

logger = logging.getLogger(__name__)
AGENT_NAME = "evaluator"

FAIRNESS_SYSTEM_PROMPT = """You are an impartial, experienced insurance claims
adjuster reviewing another adjuster's completed decision on a claim.

Your ONLY question: would a competent, unbiased human adjuster reach the
SAME conclusion (decision and amount) on this specific claim, given the
facts shown? You are not re-adjudicating from scratch and not re-checking
arithmetic - assume the numbers shown are correct. You are judging whether
the OUTCOME is a reasonable, unbiased one for these specific facts.

If the claim materials don't give you enough to judge this confidently,
return score=null (not a guess) and explain what's missing in reasoning."""

TRANSPARENCY_SYSTEM_PROMPT = """You are reviewing whether an insurance
settlement decision's reasoning is explainable and traceable.

Your ONLY question: reading the calculation breakdown and denial reasons
shown, could a claimant or a regulator follow WHY this decision was reached,
step by step, without having to guess or infer missing logic? You are not
judging whether the decision itself is correct or fair - only whether its
stated reasoning is clear and complete.

If there's nothing to evaluate (e.g. no breakdown was provided at all),
return score=null and explain what's missing in reasoning."""


def _verify_accuracy(settlement, policy_check) -> tuple[bool, str]:
    """Code-verified check: does settlement_amount_usd respect its own
    invariants? Uses settlement_output's own fields (gross_damage_usd,
    deductible_applied_usd are already code-corrected to the real values -
    see run_settlement_calculator) rather than re-deriving the formula
    independently, so this never drifts out of sync with the real
    calculation logic it's checking.
    """
    if not settlement:
        return False, "No settlement_output to verify - evaluator ran before settlement."

    payable_cap = max(0.0, settlement.gross_damage_usd - settlement.deductible_applied_usd)
    amount = settlement.settlement_amount_usd

    if amount > payable_cap + 0.01:
        return False, (
            f"settlement_amount_usd ({amount:,.2f}) exceeds damage minus deductible "
            f"({payable_cap:,.2f})"
        )

    if policy_check and policy_check.covered_amount_usd > 0 and amount > policy_check.covered_amount_usd + 0.01:
        return False, (
            f"settlement_amount_usd ({amount:,.2f}) exceeds the policy's covered amount "
            f"({policy_check.covered_amount_usd:,.2f})"
        )

    return True, (
        f"settlement_amount_usd ({amount:,.2f}) is within damage-minus-deductible "
        f"({payable_cap:,.2f}) and the policy's covered amount"
    )


def _judge_dimension(system_prompt: str, user_prompt: str) -> JudgeDimensionOutput:
    """One isolated LLM-as-judge call, graded alone - per Anthropic's
    'grade each dimension with an isolated LLM-as-judge rather than using
    one to grade all dimensions.' Fails toward 'unknown' (score=None), not
    a guessed mid-point, if the judge call itself errors - consistent with
    the schema's own 'I don't know' path, same fail-toward-caution pattern
    as src/security/injection_check.py (though this one doesn't need to
    fail CLOSED the way injection detection does - an unresolved quality
    dimension just means this claim isn't auto-released, not a security
    gap)."""
    llm = get_judge_llm().with_structured_output(JudgeDimensionOutput)
    try:
        return llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
    except Exception as e:
        logger.error(f"Judge dimension call failed: {e}")
        return JudgeDimensionOutput(score=None, reasoning=f"Judge call failed: {type(e).__name__}: {e}")


def run_evaluator(state: ClaimsState) -> dict:
    """LangGraph node for claim-decision quality evaluation."""
    cfg = get_evaluation_config()
    claim = state["claim"]
    claim_id = claim["claim_id"]
    start_time = time.time()

    # Skip evaluation based on sample rate (for batch processing efficiency)
    import random
    sample_rate = cfg.get("batch_eval_sample_rate", 1.0)

    # Always evaluate HITL claims, high-value claims, and human overrides
    from src.config import get_hitl_config
    high_value_threshold = get_hitl_config()["triggers"].get("min_amount", 10000)
    always_eval = (
        state.get("hitl_required", False)
        or float(claim.get("estimated_amount", 0)) >= high_value_threshold
        or state.get("human_override", False)
    )

    if not always_eval and random.random() > sample_rate:
        logger.info(f"[{claim_id}] Evaluation skipped (sample rate {sample_rate})")
        # `evaluation_passed: None`, NOT True - an unjudged claim must not be
        # recorded as "judged and passed" (see route_after_evaluation).
        return {
            "evaluation_passed": None,
            "evaluation_skipped": True,
            "pipeline_trace": [{"agent": AGENT_NAME, "status": "skipped_sampling"}],
        }

    logger.info(f"[{claim_id}] Evaluation started")

    masked_claim = state.get("masked_claim") or mask_claim(dict(claim))
    settlement = state.get("settlement_output")
    fraud = state.get("fraud_output")
    policy_check = state.get("policy_output")
    damage = state.get("damage_output")
    guardrails_violations = state.get("guardrails_violations", [])
    _sym = currency_symbol()

    # ── Dimension 1: ACCURACY - code-verified, not an LLM call ────────────
    accuracy_verified, accuracy_detail = _verify_accuracy(settlement, policy_check)

    settlement_summary = "No settlement computed"
    if settlement:
        settlement_summary = f"""
Decision: {settlement.decision.value}
Settlement Amount: {_sym}{settlement.settlement_amount_usd:,.2f}
Gross Damage: {_sym}{settlement.gross_damage_usd:,.2f}
Deductible Applied: {_sym}{settlement.deductible_applied_usd:,.2f}
Depreciation Applied: {_sym}{settlement.depreciation_applied_usd:,.2f}
Calculation Steps: {chr(10).join(settlement.calculation_breakdown)}
Denial Reasons: {', '.join(settlement.denial_reasons) or 'N/A'}
"""

    claim_context = f"""
=== CLAIM ===
ID: {claim_id}
Type: {claim.get('incident_type')}
Estimated Amount: {_sym}{float(claim.get('estimated_amount', 0)):,.2f}
Description (claimant-supplied, PII masked): {masked_claim.get('incident_description', 'N/A')}

=== DAMAGE ASSESSMENT ===
Assessed Amount: {_sym}{(damage.assessed_damage_usd if damage else 0):,.2f}
Recommendation: {damage.repair_vs_replace if damage else 'N/A'}
Notes: {damage.assessment_notes if damage else 'N/A'}

=== FRAUD ASSESSMENT ===
Risk Level: {fraud.fraud_risk_level.value if fraud else 'N/A'}
Fraud Score: {(fraud.fraud_score if fraud else 0):.2f}
Primary Concerns: {', '.join(fraud.primary_concerns[:3]) if fraud else 'N/A'}

=== POLICY CHECK ===
Coverage Status: {policy_check.coverage_status.value if policy_check else 'N/A'}
Exclusions Triggered: {', '.join(policy_check.exclusions_triggered) if policy_check else 'None'}

=== SETTLEMENT ===
{settlement_summary}
"""

    # ── Dimension 2: FAIRNESS - isolated judge call ────────────────────────
    fairness = _judge_dimension(
        FAIRNESS_SYSTEM_PROMPT,
        f"{claim_context}\nWould a human adjuster reach the same conclusion on this claim?",
    )

    # ── Dimension 3: TRANSPARENCY - isolated judge call ────────────────────
    transparency = _judge_dimension(
        TRANSPARENCY_SYSTEM_PROMPT,
        f"{claim_context}\nIs the reasoning above explainable and traceable, step by step?",
    )

    # ── Combine: overall_score and passed are computed by code ────────────
    dimension_scores = [s for s in (fairness.score, transparency.score) if s is not None]
    accuracy_component = 1.0 if accuracy_verified else 0.0
    if dimension_scores:
        overall_score = (accuracy_component + sum(dimension_scores)) / (1 + len(dimension_scores))
    else:
        overall_score = accuracy_component

    min_score = cfg.get("min_score_to_release", 0.70)
    passed = bool(accuracy_verified) and overall_score >= min_score

    flags: list[str] = []
    if not accuracy_verified:
        flags.append(f"accuracy_check_failed: {accuracy_detail}")
    if fairness.score is None:
        flags.append("fairness_unresolved: judge could not score confidently, requires human review")
    if transparency.score is None:
        flags.append("transparency_unresolved: judge could not score confidently, requires human review")
    # An unresolved dimension means this claim cannot be auto-released even
    # if the resolved dimensions look fine - "unknown" is not "passing".
    if fairness.score is None or transparency.score is None:
        passed = False

    output = EvaluationOutput(
        accuracy_verified=accuracy_verified,
        accuracy_detail=accuracy_detail,
        fairness=fairness,
        transparency=transparency,
        overall_score=round(overall_score, 3),
        passed=passed,
        flags=flags,
    )

    duration_ms = int((time.time() - start_time) * 1000)

    log_agent_action(
        claim_id=claim_id,
        agent_name=AGENT_NAME,
        action="claim_quality_evaluation",
        output_summary={
            "overall_score": output.overall_score,
            "passed": passed,
            "flags": output.flags,
        },
        duration_ms=duration_ms,
    )

    if not passed:
        logger.warning(
            f"[{claim_id}] Evaluation FAILED: score={output.overall_score:.2f} < {min_score:.2f}. "
            f"Flags: {output.flags}"
        )
    else:
        logger.info(f"[{claim_id}] Evaluation PASSED: score={output.overall_score:.2f}")

    return {
        "evaluation_output": output,
        "evaluation_passed": passed,
        "evaluation_skipped": False,
        "pipeline_trace": [{
            "agent": AGENT_NAME,
            "overall_score": output.overall_score,
            "passed": passed,
            "duration_ms": duration_ms,
            "confidence": output.overall_score,
            "decision": "passed" if passed else "failed",
            "reasoning": f"accuracy: {accuracy_detail} | fairness: {fairness.reasoning} | transparency: {transparency.reasoning}",
            "flags": output.flags,
            "findings": {
                "accuracy_verified": accuracy_verified,
                "fairness_score": fairness.score,
                "transparency_score": transparency.score,
            },
        }],
        "agent_call_count": 1,  # operator.add reducer sums this into the running total - see state.py
    }
