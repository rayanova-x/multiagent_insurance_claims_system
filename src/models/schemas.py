"""
Pydantic v2 schemas for structured LLM outputs.
Every agent returns one of these - zero parsing errors guaranteed.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class ClaimType(str, Enum):
    AUTO_COLLISION = "auto_collision"
    AUTO_THEFT = "auto_theft"
    PROPERTY_FIRE = "property_fire"
    PROPERTY_WATER = "property_water"
    LIABILITY = "liability"
    MEDICAL = "medical"
    UNKNOWN = "unknown"


class ClaimDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_PARTIAL = "approved_partial"
    DENIED = "denied"
    PENDING_DOCUMENTS = "pending_documents"
    ESCALATED_HITL = "escalated_human_review"
    FRAUD_INVESTIGATION = "fraud_investigation"
    AUTO_REJECTED = "auto_rejected"


class FraudRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CONFIRMED = "confirmed"


class HITLPriority(str, Enum):
    CRITICAL = "critical"   # Resolve within 4 hours
    HIGH = "high"           # Resolve within 24 hours
    NORMAL = "normal"       # Resolve within 72 hours


class CoverageStatus(str, Enum):
    COVERED = "covered"
    PARTIALLY_COVERED = "partially_covered"
    NOT_COVERED = "not_covered"
    NEEDS_VERIFICATION = "needs_verification"


# ─────────────────────────────────────────────
# Agent Output Schemas
# ─────────────────────────────────────────────

class IntakeValidationOutput(BaseModel):
    """Output from Claims Intake Agent.

    is_valid, policy_active, claimant_eligible are COMPUTED BY CODE
    (policy_active AND covered - see run_intake_agent) before the LLM is
    ever called; they are not asked of the model. The LLM's own job is
    narrower: write intake_notes. Prompt-injection detection is a SEPARATE
    call (src/security/injection_check.py), not part of this schema -
    there is deliberately no "confidence" field here anymore, since
    is_valid is no longer a judgment call with meaningful uncertainty.
    """
    is_valid: bool = Field(description="Computed by code: policy_active AND coverage exists for this claim type.")
    claim_type: ClaimType = Field(description="Detected claim type")
    policy_active: bool = Field(description="Computed by code: whether the policy is active and not lapsed.")
    claimant_eligible: bool = Field(description="Computed by code: same as policy_active for now.")
    missing_documents: list[str] = Field(default_factory=list, description="List of required but missing documents")
    intake_notes: str = Field(description="Plain-language summary of the intake findings, written by the model.")
    validation_flags: list[str] = Field(default_factory=list, description="Any flags raised during intake")


class FraudPatternOutput(BaseModel):
    """Output from CrewAI Pattern Analyst."""
    pattern_matches: list[str] = Field(description="Fraud patterns matched from database")
    risk_indicators: list[str] = Field(description="Specific fraud indicators detected")
    pattern_score: float = Field(ge=0.0, le=1.0, description="Pattern-based fraud probability 0-1")
    analysis: str = Field(description="Detailed pattern analysis")


class AnomalyDetectionOutput(BaseModel):
    """Output from CrewAI Anomaly Detector."""
    statistical_anomalies: list[str] = Field(description="Statistical outliers detected")
    claim_frequency_flag: bool = Field(description="True if claimant has unusually high claim frequency")
    amount_anomaly: bool = Field(description="True if amount deviates significantly from similar claims")
    timing_anomaly: bool = Field(description="True if claim timing is suspicious (e.g. policy just purchased)")
    anomaly_score: float = Field(ge=0.0, le=1.0, description="Anomaly-based fraud probability 0-1")
    analysis: str = Field(description="Detailed anomaly analysis")


class SocialValidationOutput(BaseModel):
    """Output from CrewAI Social Validator."""
    story_consistent: bool = Field(description="Whether the claimant's account is internally consistent")
    inconsistencies: list[str] = Field(description="Specific inconsistencies found in the claim narrative")
    identity_flags: list[str] = Field(description="Identity verification concerns")
    validation_score: float = Field(ge=0.0, le=1.0, description="Story consistency score 0-1 (higher = more consistent)")
    analysis: str = Field(description="Detailed validation analysis")


class FraudAssessmentOutput(BaseModel):
    """Final output from the full CrewAI Fraud Detection Crew."""
    fraud_risk_level: FraudRiskLevel = Field(description="Overall fraud risk classification")
    fraud_score: float = Field(ge=0.0, le=1.0, description="Composite fraud probability 0-1")
    primary_concerns: list[str] = Field(description="Top fraud concerns to highlight")
    recommendation: str = Field(description="Recommended action: proceed | escalate | reject")
    crew_summary: str = Field(description="Synthesized findings from all crew members")
    pattern_score: float = Field(ge=0.0, le=1.0)
    anomaly_score: float = Field(ge=0.0, le=1.0)
    consistency_score: float = Field(ge=0.0, le=1.0, description="Consistency Validator's validation_score (1 = fully consistent story)")
    narrative_risk: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="1 - consistency_score, or None if the crew's narrative verdict could not be parsed",
    )
    fraud_admission: Optional[bool] = Field(
        None,
        description="Validator found the claimant's own words state an intent to deceive "
                    "(None = not reported). Required for auto-reject.",
    )
    confidence: float = Field(
        0.5, ge=0.0, le=1.0,
        description="How much the crew's own assessment can be trusted: all three signals read = high, "
                    "narrative unparsed = low, crew error = very low. Drives the fraud_crew confidence gate.",
    )


class DamageAssessmentOutput(BaseModel):
    """Output from Damage Assessment Agent."""
    assessed_damage_usd: float = Field(ge=0.0, description="Total assessed damage in USD")
    line_items: list[dict] = Field(description="Itemized damage breakdown")
    repair_vs_replace: str = Field(description="Recommendation: repair | replace | total_loss")
    assessment_confidence: float = Field(ge=0.0, le=1.0)
    assessment_notes: str = Field(description="Assessor's notes and methodology")
    requires_physical_inspection: bool = Field(description="Whether a physical inspector should visit")
    comparable_claims_avg: Optional[float] = Field(None, description="Average payout for comparable claims")


class PolicyCheckOutput(BaseModel):
    """Output from Policy Compliance Agent.

    policy_checker runs as a parallel fan-out branch (alongside
    damage_assessor and fraud_crew - see src/agents/graph.py) and does NOT
    have access to the real damage figure at the time it runs. deductible_usd
    IS computed by this agent's own code (pure arithmetic on the policy
    record, no damage figure needed). covered_amount_usd is NOT computed
    here at all - it is a placeholder until graph.py's post-join step
    recomputes it from the real damage_output once the fan-out completes.
    Never read policy_output.covered_amount_usd as final before that step
    has run. coverage_status is forced to NOT_COVERED by code when the claim
    type has no coverage at all (a fact already known before the LLM is
    called); the LLM's genuine judgment is choosing among COVERED/
    PARTIALLY_COVERED/NEEDS_VERIFICATION, and identifying which exclusions
    apply from the claim narrative - things code cannot determine on its own.
    """
    coverage_status: CoverageStatus
    covered_amount_usd: float = Field(ge=0.0, description="PLACEHOLDER until graph.py's post-join step recomputes this from the real damage figure. Not meaningful before then.")
    deductible_usd: float = Field(ge=0.0, description="Computed by code from the policy's deductible for this coverage type.")
    exclusions_triggered: list[str] = Field(default_factory=list, description="Policy exclusions that apply, per the explicit per-exclusion criteria in the prompt")
    coverage_notes: str = Field(description="Explanation of coverage determination")
    compliance_flags: list[str] = Field(default_factory=list, description="Regulatory compliance concerns")
    policy_limits: dict = Field(description="Applicable policy limits")
    confidence: float = Field(ge=0.0, le=1.0)


class ConflictType(str, Enum):
    """The two specific, named cross-branch conflict types the consistency
    coordinator checks for (src/agents/consistency_coordinator.py) - a
    deliberately narrow, explicit list, not an open-ended "do these seem
    inconsistent" judgment. Each type was chosen because it's structurally
    invisible to any single existing agent, confirmed by reading what each
    agent actually has in its own prompt context (not assumed):
      - MISSED_FACT: policy_checker never reads damage_output at all (by
        the fan-out's own design), so a fact damage's notes/line_items
        surface that bears on an exclusion judgment (e.g. commercial-use
        markings) can never reach policy_checker on its own.
      - SEVERITY_MISMATCH: damage_assessor never reads fraud_output (nor
        vice versa) - both run fully isolated in the same fan-out step -
        so a real disagreement between damage's computed severity and
        fraud's narrative-consistency read (e.g. damage says total_loss,
        fraud's narrative excerpt describes "minor damage") is invisible
        to both agents individually. A third, explicitly considered
        candidate - repair-cost-to-narrative plausibility - was DROPPED:
        damage_assessor already receives both the incident description AND
        the repair line items in the SAME prompt call (confirmed by
        reading damage_assessor.py directly), and its own Rule 3 (40%
        deviation cap) already acts on exactly that comparison - a
        coordinator re-checking it would be re-grading a question already
        asked and answered by hard rule, not seeing anything new.
    """
    MISSED_FACT = "missed_fact"
    SEVERITY_MISMATCH = "severity_mismatch"


class ConflictSeverity(str, Enum):
    """Tied to CONSEQUENCE, not vagueness - a deliberate alternative to a
    free-floating confidence float (consistent with this project's standing
    move away from confidence-as-a-judgment-signal - see intake's is_valid
    and policy's coverage_status, both of which dropped free-floating
    confidence in favor of fact/consequence-based fields where possible).
    MATERIAL: the conflict could plausibly change the payout, coverage
    decision, or fraud classification if resolved differently - this is
    the only severity that triggers a retry.
    NOTABLE: affects explanation quality / confidence in the record, but
    would not change the actual decision even if resolved differently -
    logged and surfaced in the HITL brief if the claim pauses for any
    other reason, never triggers a retry on its own.
    """
    MATERIAL = "material"
    NOTABLE = "notable"


class ConflictFinding(BaseModel):
    """One specific conflict the coordinator found between two branches'
    finished, isolated outputs."""
    conflict_type: ConflictType
    severity: ConflictSeverity
    description: str = Field(description="Plain-language statement of the specific contradiction, citing the actual facts from both sides.")
    reasoning: str = Field(description="Why this rises to MATERIAL or only NOTABLE - reference the real insurance consequence (payout/coverage/fraud classification), not just that the texts differ.")
    retry_target: Optional[str] = Field(
        None, description="Which agent should be retried with this finding as new context: 'policy_checker' for MISSED_FACT, 'damage_assessor' for SEVERITY_MISMATCH. None if severity is NOTABLE.",
    )


class ConsistencyCheckOutput(BaseModel):
    """Output from the consistency coordinator (src/agents/
    consistency_coordinator.py) - a single isolated LLM call that reads
    damage_assessor's, policy_checker's, and fraud_crew's already-finished
    outputs together and checks ONLY for the two conflict types named in
    ConflictType. Does not re-adjudicate any branch's own judgment calls
    (e.g. it never second-guesses whether an exclusion criterion was
    applied correctly) - only whether the branches' findings are mutually
    consistent with each other.

    has_conflicts / conflicts are the LLM's genuine judgment (reading two
    isolated texts and recognizing a contradiction requires real language
    understanding - this is not arithmetic code could do). Whether to
    actually RETRY is computed by code in graph.py from conflicts' severity
    and the configured retry cap, never decided by this LLM call itself -
    same "code decides routing, LLM only judges" pattern as every other
    agent in this pipeline.
    """
    has_conflicts: bool
    conflicts: list[ConflictFinding] = Field(default_factory=list)
    summary: str = Field(description="One or two sentences summarizing what was checked and found, for the audit trail.")


class SettlementOutput(BaseModel):
    """Output from Settlement Calculator Agent.

    gross_damage_usd, deductible_applied_usd, and depreciation_applied_usd
    are COMPUTED BY CODE (run_settlement_calculator overwrites them with the
    real assessed_damage/deductible/depreciation values after the LLM
    responds) - these are facts code already has, not judgment calls.
    settlement_amount_usd is also code-capped (max payout multiplier,
    coverage limit, deductible floor, fraud-score denial) but the LLM's
    initial estimate still matters since the caps only clamp it, they don't
    replace it outright.
    """
    decision: ClaimDecision
    settlement_amount_usd: float = Field(ge=0.0)
    gross_damage_usd: float = Field(ge=0.0, description="Damage before deductions. Computed by code from the assessed damage.")
    deductible_applied_usd: float = Field(ge=0.0, description="Computed by code from the policy's deductible.")
    depreciation_applied_usd: float = Field(ge=0.0, description="Computed by code from the depreciation tool.")
    calculation_breakdown: list[str] = Field(description="Step-by-step calculation, max 6 steps")
    denial_reasons: list[str] = Field(default_factory=list, description="Reasons if denied")
    confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    regulatory_compliance: bool = Field(default=True, description="Passes state regulatory requirements")


class JudgeDimensionOutput(BaseModel):
    """One isolated LLM-as-judge call's verdict on a single dimension.

    Per Anthropic's "Demystifying evals for AI agents": grade each
    dimension with an ISOLATED judge call, not one call scoring several
    dimensions at once - bundling dimensions into a single call is an
    anti-pattern independent of whether the dimensions overlap. score is
    Optional (not a forced float) so the judge has a genuine "I don't
    know" path per CLAUDE.md's "schemas must have a legitimate I don't
    know path" - Anthropic's guidance says the same thing explicitly:
    give the judge an escape hatch to avoid hallucinating a score when it
    lacks enough information to judge confidently.
    """
    score: Optional[float] = Field(None, ge=0.0, le=1.0, description="None when there isn't enough information to judge confidently - do not guess.")
    reasoning: str = Field(description="Reasoning written BEFORE any score is implied - required even when score is None, explaining what's missing.")


class EvaluationOutput(BaseModel):
    """Output from the claim-decision quality evaluator.

    Only FAIRNESS and TRANSPARENCY are graded by an LLM judge (each its
    own isolated call - see JudgeDimensionOutput). The other three
    dimensions the original 5-dimension design asked an LLM to grade
    (ACCURACY, SAFETY, COMPLETENESS) turned out to be mostly redundant
    with facts code already guarantees upstream before this evaluator
    ever runs: ACCURACY is settlement math, already code-capped in
    settlement_calculator; SAFETY is the fraud-score code-enforced denial
    and guard_node's own halts; COMPLETENESS is policy_checker iterating
    the policy's actual exclusion list in code. Asking an LLM to re-grade
    a fact code already guarantees is asking it to verify arithmetic by
    reading prose - the same "schema validity is not correctness" gap
    found and fixed elsewhere in this codebase. Per CLAUDE.md's
    "multi-agent needs one of three specific justifications" (context
    isolation, real parallelism, or independent verification with a
    fresh context) - FAIRNESS and TRANSPARENCY are the two dimensions
    that actually need a fresh, independent read: would a human adjuster
    reach the same conclusion, and is the reasoning explainable. Neither
    reduces to a formula, unlike the other three.

    accuracy_verified is computed BY CODE (run_evaluator recomputes the
    expected settlement from the real inputs and compares) - not an LLM
    judgment call.
    """
    accuracy_verified: bool = Field(description="Computed by code: does settlement_amount_usd match the formula result from the real inputs.")
    accuracy_detail: str = Field(description="Computed by code: what was compared and what the expected vs actual values were.")
    fairness: JudgeDimensionOutput = Field(description="Isolated judge call: would a human adjuster reach the same conclusion on this specific claim?")
    transparency: JudgeDimensionOutput = Field(description="Isolated judge call: is the settlement's reasoning explainable and traceable?")
    overall_score: float = Field(ge=0.0, le=1.0, description="Computed by code from accuracy_verified + fairness.score + transparency.score, not an LLM judgment.")
    passed: bool = Field(description="Whether the decision passes the quality gate - computed by code against the configured threshold.")
    flags: list[str] = Field(default_factory=list, description="Critical issues requiring immediate attention - includes a flag when any judge dimension returned score=None (unresolved, needs a human).")


class CommunicationOutput(BaseModel):
    """Output from Communication Agent."""
    subject: str = Field(description="Email/notification subject line")
    message: str = Field(description="Full notification message to claimant")
    internal_notes: str = Field(description="Internal adjuster notes (not sent to claimant)")
    next_steps: list[str] = Field(description="Action items for claimant")
    appeal_instructions: Optional[str] = Field(None, description="How to appeal if denied")
