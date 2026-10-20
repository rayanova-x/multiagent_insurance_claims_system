"""
HITL (Human-In-The-Loop) Checkpoint - determines whether a claim needs human review.

Trigger logic:
  1. Check each trigger rule against current state
  2. Compute priority score (0-100) using weighted factors
  3. Classify priority: critical (>=80), high (60-79), normal (<60)
  4. Return list of triggered reasons + priority

This is a pure function - no LLM calls, no side effects.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.config import get_confidence_gate_config, get_hitl_config
from src.models.schemas import (
    DamageAssessmentOutput,
    FraudAssessmentOutput,
    HITLPriority,
    IntakeValidationOutput,
    PolicyCheckOutput,
)

logger = logging.getLogger(__name__)


def check_hitl_required(
    claim: dict,
    intake_output: Optional[IntakeValidationOutput],
    fraud_output: Optional[FraudAssessmentOutput],
    damage_assessed_usd: float = 0.0,
    claim_history_count: int = 0,
    agent_confidence_scores: Optional[list[float]] = None,
    damage_output: Optional[DamageAssessmentOutput] = None,
    policy_output: Optional[PolicyCheckOutput] = None,
) -> tuple[bool, list[str], HITLPriority, float]:
    """
    Evaluate all HITL triggers.

    Returns:
        (requires_hitl, trigger_reasons, priority, priority_score)
    """
    cfg = get_hitl_config()
    triggers_cfg = cfg["triggers"]
    weights = cfg["priority_weights"]

    from src.utils import currency_symbol
    _sym = currency_symbol()

    triggers: list[str] = []
    priority_components: dict[str, float] = {}

    estimated_amount = float(claim.get("estimated_amount", 0))
    is_appeal = claim.get("is_appeal", False)

    # ── Trigger 1: High claim amount ──────────────────────────────────────────
    min_amount = triggers_cfg.get("min_amount", 10000)
    if estimated_amount >= min_amount:
        triggers.append(f"Claim amount {_sym}{estimated_amount:,.0f} exceeds review threshold {_sym}{min_amount:,.0f}")
        priority_components["amount"] = min(estimated_amount / min_amount / 5, 1.0)
    else:
        priority_components["amount"] = 0.0

    # ── Trigger 2: Fraud score ────────────────────────────────────────────────
    fraud_threshold = triggers_cfg.get("fraud_score", 0.65)
    fraud_score = 0.0
    if fraud_output:
        fraud_score = fraud_output.fraud_score
        if fraud_score >= fraud_threshold:
            triggers.append(
                f"Fraud score {fraud_score:.2f} exceeds threshold {fraud_threshold:.2f} "
                f"(risk: {fraud_output.fraud_risk_level.value})"
            )
        priority_components["fraud_score"] = fraud_score
    else:
        priority_components["fraud_score"] = 0.0

    # ── Confidence: priority-score input ONLY, never a trigger ───────────────
    # Low agent confidence must not by itself queue a claim for human review
    # (a confident hallucination scores high exactly where this would matter
    # most). Once a claim is already queued by one of the property-based
    # triggers above, low confidence here raises its priority within that
    # queue - it cannot add a claim to the queue on its own. See the
    # confidence_gates comment in configs/base.yaml.
    low_conf_floor = get_confidence_gate_config().get("priority_confidence_floor", 0.65)
    if agent_confidence_scores:
        avg_confidence = sum(agent_confidence_scores) / len(agent_confidence_scores)
        priority_components["confidence"] = max(0, low_conf_floor - avg_confidence) / low_conf_floor
    else:
        priority_components["confidence"] = 0.0

    # ── Trigger 4: First-time claimant + high value ───────────────────────────
    first_claim_threshold = triggers_cfg.get("first_claim_high_value", 5000)
    if claim_history_count == 0 and estimated_amount >= first_claim_threshold:
        triggers.append(
            f"First-time claimant with high-value claim ({_sym}{estimated_amount:,.0f})"
        )
        priority_components["repeat_claimant"] = 0.5
    # ── Trigger 5: Repeat claimant (possible abuse) ───────────────────────────
    elif claim_history_count > 2:
        triggers.append(f"Claimant has {claim_history_count} prior claims in review window")
        priority_components["repeat_claimant"] = min(claim_history_count / 5, 1.0)
    else:
        priority_components["repeat_claimant"] = 0.0

    # ── Trigger 6: Inconsistent documents / intake flags ──────────────────────
    # "LLM error - requires manual review" is a severe, distinct case: it means
    # both the primary and fallback model failed after their retries (see
    # src/llm.py), not an ordinary low-confidence judgment call. Surface it as
    # its own named trigger rather than folding it into the generic flags
    # list, so a reviewer understands immediately why this ticket exists.
    if intake_output and intake_output.validation_flags:
        llm_failure_flags = [f for f in intake_output.validation_flags if "LLM error" in f]
        other_flags = [f for f in intake_output.validation_flags if "LLM error" not in f]
        if llm_failure_flags:
            triggers.append(
                "Intake LLM unavailable - validity decided by rule-based checks only, manual review required"
            )
        if other_flags:
            triggers.append(f"Intake flags: {', '.join(other_flags)}")

    # ── Trigger 7: Appeal workflow always gets human review ───────────────────
    if is_appeal:
        triggers.append("Appeals are always reviewed by a human adjuster")

    # ── Trigger 8: Intake validation failures ─────────────────────────────────
    if intake_output and not intake_output.is_valid:
        triggers.append("Claim failed initial intake validation")

    # ── Trigger 9: Damage assessor requires physical inspection ──────────────
    # Property-based, regardless of amount: the assessor (including its
    # LLM-failure fallback, which also sets this True) is saying it cannot
    # reliably assess the damage from documents alone. Previously this field
    # was write-only - nothing read it to actually route anywhere.
    if damage_output and damage_output.requires_physical_inspection:
        triggers.append("Damage assessor requires physical inspection before a decision")

    # ── Trigger 10: Policy exclusions or compliance concerns ──────────────────
    # Property-based, regardless of amount: a genuine exclusion or compliance
    # finding is a concrete, specific problem the model identified - more
    # consequential than low confidence, not less. Also catches the LLM
    # outage marker ("LLM error - manual verification recommended") appended
    # to compliance_flags on policy_checker's failure fallback. Previously
    # these fields were write-only - only shown to the evaluator/judge as
    # text, never read to actually route anywhere.
    if policy_output and (policy_output.exclusions_triggered or policy_output.compliance_flags):
        reasons = policy_output.exclusions_triggered + policy_output.compliance_flags
        triggers.append(f"Policy exclusions/compliance concerns: {', '.join(reasons)}")

    # ── Trigger 11: Claimant disputed the suggested incident_type ─────────────
    # Property-based, regardless of amount: the /verify-type check
    # (src/security/incident_type_check.py) suggested a different type than
    # the claimant selected, and the claimant chose to keep their own
    # selection anyway. Neither side should silently win - surface the
    # claimant's own stated reason so the reviewer sees both sides.
    if claim.get("type_disputed_by_claimant"):
        reason = claim.get("type_dispute_reason") or "no reason given"
        triggers.append(f"Claimant disputed suggested incident_type - claimant's stated reason: {reason}")

    # ── Priority Score Calculation ────────────────────────────────────────────
    priority_score = (
        priority_components.get("amount", 0.0) * weights.get("amount", 0.30) +
        priority_components.get("fraud_score", 0.0) * weights.get("fraud_score", 0.35) +
        priority_components.get("confidence", 0.0) * weights.get("confidence", 0.20) +
        priority_components.get("repeat_claimant", 0.0) * weights.get("repeat_claimant", 0.15)
    ) * 100  # Scale to 0-100

    # Always CRITICAL if appeal
    if is_appeal:
        priority_score = max(priority_score, 80)

    # ── Priority Classification ───────────────────────────────────────────────
    if priority_score >= 80:
        priority = HITLPriority.CRITICAL
    elif priority_score >= 60:
        priority = HITLPriority.HIGH
    else:
        priority = HITLPriority.NORMAL

    requires_hitl = len(triggers) > 0

    if requires_hitl:
        logger.info(
            f"HITL required for claim {claim.get('claim_id')} | "
            f"Priority: {priority.value} ({priority_score:.1f}) | "
            f"Triggers: {len(triggers)}"
        )

    return requires_hitl, triggers, priority, round(priority_score, 1)


def format_hitl_brief(
    claim: dict,
    triggers: list[str],
    priority: HITLPriority,
    fraud_output: Optional[FraudAssessmentOutput],
    damage_assessed_usd: float = 0.0,
) -> str:
    """
    Generate a concise brief for the human reviewer.
    Shown at the top of the HITL review interface.
    """
    fraud_section = ""
    if fraud_output:
        fraud_section = (
            f"\nFRAUD RISK: {fraud_output.fraud_risk_level.value.upper()} "
            f"(score: {fraud_output.fraud_score:.2f})\n"
            f"Top Concerns: {', '.join(fraud_output.primary_concerns[:3]) if fraud_output.primary_concerns else 'None'}"
        )

    from src.utils import currency_symbol
    sym = currency_symbol()

    return f"""
=== HUMAN REVIEW BRIEF ===
Claim: {claim.get('claim_id')} | Policy: {claim.get('policy_number')}
Type: {claim.get('incident_type', 'UNKNOWN').upper()} | Amount: {sym}{float(claim.get('estimated_amount', 0)):,.2f}
AI Damage Assessment: {sym}{damage_assessed_usd:,.2f}
Priority: {priority.value.upper()}
{fraud_section}

REVIEW TRIGGERS:
{chr(10).join(f'  - {t}' for t in triggers)}

Incident: {claim.get('incident_description', 'N/A')}
Documents: {', '.join(claim.get('documents', [])) or 'None provided'}
========================
""".strip()
