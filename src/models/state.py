"""
LangGraph state definition for the Smart Claims Processor.

Uses TypedDict with Annotated lists for append-only parallel-safe state.
All agent outputs are typed; missing fields default to None.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict


def _merge_run_versions(current: Optional[dict], new: Optional[dict]) -> dict:
    """Reducer for run_versions: a plain dict field would raise
    InvalidUpdateError the moment two parallel fan-out branches (damage_
    assessor, policy_checker, fraud_crew - see src/agents/graph.py) both
    write to it in the same super-step, same as any other un-reduced field.
    Dicts can't use operator.add (that raises TypeError on dicts), so this
    merges them field by field instead - mirrors run_versions_update's own
    "merge rather than replace" logic in src/provenance.py, just applied
    across CONCURRENT branches instead of across sequential pauses/resumes.
    "prompts" and "retrieved" are themselves per-node dicts (keyed by node
    name), so merging them key-by-key is safe - two different branches
    always write under two different node-name keys, never the same one.
    """
    current = dict(current or {})
    new = dict(new or {})
    merged = {**current, **new}
    for nested_key in ("prompts", "retrieved"):
        merged_nested = dict(current.get(nested_key) or {})
        merged_nested.update(new.get(nested_key) or {})
        if merged_nested:
            merged[nested_key] = merged_nested
    return merged


def _prefer_non_none(current: Optional[Any], new: Optional[Any]) -> Optional[Any]:
    """Reducer for fields where, in the normal (non-conflict) case, EVERY
    concurrent fan-out branch writes the same "clear" value (None) - and
    only in the rare retry case does exactly one branch write a real value.
    A plain field raises InvalidUpdateError the moment two branches both
    write it in the same super-step, even when both write None (confirmed
    by a real crash: consistency_retry_context is set by
    prepare_consistency_retry_node for ONE retry-eligible agent, then
    cleared back to None by BOTH damage_assessor and policy_checker on
    their next completion, including the one that was never retried -
    operator.add can't be used since you can't add a string to None).
    `new` wins whenever it's non-None; otherwise keep whatever `current`
    already was - this is NOT a true merge (there is no real scenario
    where two branches both have genuinely different non-None values to
    report at once, since only one agent is ever retried with new context
    per round), just a safe way to let concurrent identical clears and a
    single real write coexist in the same super-step.
    """
    return new if new is not None else current


from src.models.schemas import (
    ClaimDecision,
    ConsistencyCheckOutput,
    DamageAssessmentOutput,
    EvaluationOutput,
    FraudAssessmentOutput,
    HITLPriority,
    IntakeValidationOutput,
    PolicyCheckOutput,
    SettlementOutput,
    CommunicationOutput,
)


class ClaimInput(TypedDict):
    """Raw claim data as submitted by claimant."""
    claim_id: str
    policy_number: str
    claimant_name: str
    claimant_email: str
    claimant_phone: str
    claimant_dob: str
    incident_date: str
    incident_type: str
    incident_description: str
    incident_location: str
    police_report_number: Optional[str]
    estimated_amount: float
    vehicle_year: Optional[int]
    vehicle_make: Optional[str]
    vehicle_model: Optional[str]
    documents: list[str]
    is_appeal: bool
    original_claim_id: Optional[str]    # Set when this is an appeal
    # Set when /verify-type suggested a different incident_type than the
    # claimant selected, and the claimant chose to keep their own selection
    # anyway (see api/routes_claims.py verify_incident_type, src/security/
    # incident_type_check.py). Used by route_after_intake to force a human
    # review of the disagreement, since neither the claimant's choice nor
    # the model's suggestion should silently win. Defaults off for claims
    # that never went through the verify step (e.g. test/seeded claims).
    type_disputed_by_claimant: Optional[bool]
    type_dispute_reason: Optional[str]


class ClaimsState(TypedDict):
    """
    Full pipeline state. Fields are populated incrementally as agents run.
    append_log uses operator.add so parallel agents can each push trace entries.
    """

    # ── Input ────────────────────────────────────────────────────────────
    claim: ClaimInput
    masked_claim: Optional[dict]        # PII-masked version sent to LLMs
    # Structured document fields (src/models/documents.py's AutoCollisionDocuments,
    # stored as a dict) when available - e.g. from data/stratified_claims/
    # *_documents.json. None when no parsed document content exists for this
    # claim (the real submission API does not produce this yet - only
    # filenames; see CLAUDE.md "Document extraction"). Agents that can use
    # document-derived figures (e.g. damage_assessor's repair-estimate
    # stated_total) must fall back to claim-level fields when this is None,
    # never assume it is populated.
    claim_documents: Optional[dict]

    # ── Intake ───────────────────────────────────────────────────────────
    intake_output: Optional[IntakeValidationOutput]

    # ── Fraud Detection (CrewAI) ─────────────────────────────────────────
    fraud_output: Optional[FraudAssessmentOutput]

    # ── Damage Assessment ────────────────────────────────────────────────
    damage_output: Optional[DamageAssessmentOutput]

    # ── Policy Check ─────────────────────────────────────────────────────
    policy_output: Optional[PolicyCheckOutput]

    # ── Consistency Coordinator ──────────────────────────────────────────
    # Runs once after the damage/policy/fraud fan-out joins - see
    # src/agents/consistency_coordinator.py and route_after_fan_out's
    # retry-loop handling in graph.py.
    consistency_output: Optional[ConsistencyCheckOutput]
    # How many times EACH retry-eligible agent (damage_assessor,
    # policy_checker) has already been retried for a consistency conflict,
    # keyed by agent name - e.g. {"policy_checker": 1}. Code-enforced cap
    # (agents.consistency_coordinator.max_retries in base.yaml) reads this
    # to force HITL once exhausted, rather than looping indefinitely -
    # same "always bound the loop" principle as every evaluator-optimizer
    # pattern in this codebase.
    consistency_retry_counts: Optional[dict]
    # Set by graph.py immediately before routing back to a retry-eligible
    # agent (damage_assessor or policy_checker) for a consistency conflict -
    # the specific new fact/contradiction for that agent to weigh, read by
    # that agent's own prompt-building code and appended to its normal
    # context. Cleared again once read, so a later NON-retry run of the
    # same agent (e.g. a fresh claim reusing a thread id, or this field
    # simply never having been set) never sees stale context from an
    # unrelated conflict. Custom reducer (not a plain field - confirmed by
    # a real crash): BOTH damage_assessor and policy_checker write this
    # field on every completion (to clear it after use), including the one
    # that was never retried - a plain field raises InvalidUpdateError the
    # moment two concurrent branches both write it in the same super-step,
    # even when both write None. See _prefer_non_none above.
    consistency_retry_context: Annotated[Optional[str], _prefer_non_none]
    # Which agent prepare_consistency_retry_node just prepared context for -
    # read once by route_after_prepare_consistency_retry's own dispatch,
    # not meant to be read anywhere else.
    _consistency_retry_target: Optional[str]

    # ── Settlement ───────────────────────────────────────────────────────
    settlement_output: Optional[SettlementOutput]

    # ── Evaluation ───────────────────────────────────────────────────────
    evaluation_output: Optional[EvaluationOutput]
    # None = the sampler skipped this claim, so it was never judged. Distinct
    # from False (judged and failed) and True (judged and passed).
    evaluation_passed: Optional[bool]
    evaluation_skipped: Optional[bool]

    # ── Communication ────────────────────────────────────────────────────
    communication_output: Optional[CommunicationOutput]

    # ── HITL ─────────────────────────────────────────────────────────────
    hitl_required: bool
    hitl_triggers: list[str]            # Reasons HITL was triggered
    hitl_priority: Optional[HITLPriority]
    hitl_priority_score: Optional[float]
    hitl_ticket_id: Optional[str]
    human_decision: Optional[str]       # Decision entered by human reviewer
    human_reviewer_id: Optional[str]
    human_notes: Optional[str]
    human_override: bool                # True if human overrode AI recommendation
    human_settlement_override_usd: Optional[float]  # amount a reviewer set; settlement must not replace it

    # ── Final Decision ───────────────────────────────────────────────────
    final_decision: Optional[ClaimDecision]
    final_amount_usd: Optional[float]

    # ── Guardrails ───────────────────────────────────────────────────────
    guardrails_passed: bool
    guardrails_halted: bool             # True = a hard budget/timeout breach stopped the agents
    # operator.add, not a plain list: guard_node (wraps every agent,
    # including the three parallel fan-out branches - see agent_call_count's
    # comment below) writes this on every node. A plain list field raises
    # InvalidUpdateError the moment two concurrent branches both write it in
    # the same super-step. guard_node now writes only THIS node's new
    # violations (a short list, often empty), not the whole accumulated
    # history re-read-and-extended - the reducer does the accumulating.
    guardrails_violations: Annotated[list[str], operator.add]
    # Annotated with operator.add (not a plain int) because damage_assessor,
    # policy_checker, and fraud_crew now run as parallel fan-out branches
    # (see route_after_intake / the join node before settlement_calculator):
    # each returns its own "+1" (or "+3" for fraud_crew's three CrewAI
    # sub-agents) relative to 0, and LangGraph SUMS concurrent branches'
    # values for an int field with this reducer instead of letting one
    # branch's write silently clobber the others (which a plain `int` field
    # would do - only the last-merged branch's count would survive). Every
    # agent's own return value must therefore be its OWN call count for this
    # step only (e.g. "1", not "state['agent_call_count'] + 1") when it can
    # run as part of a fan-out; see each agent's own comment at its return
    # site for whether it still reads the prior total (sequential nodes
    # before/after the fan-out) or returns a bare per-step count (fan-out
    # branches).
    agent_call_count: Annotated[int, operator.add]
    # Same reasoning and the same fix as agent_call_count directly above:
    # guard_node (src/guardrails/manager.py) writes these on every node,
    # including the three parallel fan-out branches. Changed from plain
    # int/float fields to operator.add, and guard_node now writes each
    # node's own per-step delta (tokens/cost/time spent in THIS node call),
    # never a running total it read and added to - the reducer sums
    # concurrent branches' deltas instead of letting one silently clobber
    # the others (which caused a real, confirmed InvalidUpdateError the
    # first time three branches ran concurrently and all wrote these keys
    # in the same super-step - see project memory for the live reproduction).
    total_tokens_used: Annotated[int, operator.add]
    total_cost_usd: Annotated[float, operator.add]
    processing_seconds: Annotated[float, operator.add]  # active agent time (excludes time paused for a reviewer)
    execution_start_time: Optional[str]
    # Custom merge reducer (not operator.add - dicts can't be summed):
    # merges prompts/retrieved sub-dicts key-by-key rather than letting one
    # concurrent branch's whole-dict write clobber another's. See
    # _merge_run_versions above.
    run_versions: Annotated[Optional[dict], _merge_run_versions]

    # ── Audit & Tracing ──────────────────────────────────────────────────
    pipeline_trace: Annotated[list[dict], operator.add]   # Append-only log
    error_log: Annotated[list[str], operator.add]         # Append-only errors


def initial_state(claim: ClaimInput, documents: Optional[dict] = None) -> ClaimsState:
    """Build a fresh state from a raw claim input.

    `documents` is the parsed AutoCollisionDocuments content (as a dict),
    when available - optional, defaults to None so claims without parsed
    document content (the normal case today - see claim_documents' field
    comment) behave exactly as before this field was added.

    Most fields start as None (TypedDict Optional fields). Only non-None
    defaults and required fields are listed explicitly.
    """
    state: dict = {
        "claim": claim,
        "claim_documents": documents,
        # Booleans that default to False
        "hitl_required": False,
        "human_override": False,
        "guardrails_passed": True,
        "guardrails_halted": False,
        # Numeric defaults
        "agent_call_count": 0,
        "total_tokens_used": 0,
        "total_cost_usd": 0.0,
        "processing_seconds": 0.0,
        # Append-only lists (operator.add needs a starting list)
        "hitl_triggers": [],
        "guardrails_violations": [],
        "pipeline_trace": [],
        "error_log": [],
    }
    # All remaining Optional fields default to None
    for key in ClaimsState.__annotations__:
        if key not in state:
            state[key] = None
    return ClaimsState(**state)
