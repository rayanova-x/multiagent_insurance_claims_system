"""
LangGraph Main Workflow - Smart Claims Processor

Orchestrates all agents in a conditional, state-driven pipeline.

Workflow paths:

PATH A - Normal (low fraud):
  intake -> fraud_crew -> damage_assessor -> policy_checker
  -> settlement -> evaluator -> communication

PATH B - HITL Required (fraud score >= hitl.triggers.fraud_score):
  intake -> fraud_crew -> hitl_checkpoint -> [paused for reviewer]
    approved: -> damage_assessor -> policy_checker -> settlement -> evaluator -> communication
    denied / fraud_investigation / other: -> communication

PATH B2 - HITL Required (evaluator quality gate failed, or a high-value claim
          that needs sign-off on the proposed settlement):
  intake -> fraud_crew -> damage_assessor -> policy_checker
  -> settlement -> evaluator -> hitl_checkpoint -> [paused] -> communication

PATH C - Auto-Reject (confirmed fraud: composite score >= auto_reject_threshold,
         narrative judged fabricated, AND the claimant's own words admit intent):
  intake -> fraud_crew -> auto_reject -> communication

PATH D - Intake Failure (invalid claim):
  intake -> [invalid] -> communication (denial)

PATH E - Fast Mode (amount < pipeline.fast_mode.max_amount):
  intake -> settlement -> evaluator -> communication

Confidence gates (risk-stratified, not confidence-alone): an agent whose
confidence is below its confidence_gates.per_agent threshold pauses the
pipeline ONLY when the claim amount is also >= hitl.triggers.min_amount
("high-impact") -> hitl_after_<agent> [paused] -> next agent. Below that
amount, low confidence never pauses the pipeline by itself - it still feeds
the HITL priority-score "confidence" component if the claim is queued for
some other reason. Confidence must never be the sole reason a claim is
queued; see the confidence_gates comment in configs/base.yaml.

Guardrail halt: a hard budget or timeout breach before any agent ->
hitl_checkpoint [paused] -> communication (template letter, no further LLM calls).

Conditional routing functions determine which path to take at each junction.
"""

from __future__ import annotations  # for Python 3.10 compatibility with forward references in type hints

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

# SqliteSaver lives in the separate `langgraph-checkpoint-sqlite` package.
# Fall back to MemorySaver if it isn't installed - the pipeline still works,
# the only thing lost is durability across restarts of a paused (HITL) claim.
try:
    from langgraph.checkpoint.sqlite import SqliteSaver   # type: ignore
    _HAS_SQLITE_SAVER = True
except ImportError:
    from langgraph.checkpoint.memory import MemorySaver
    SqliteSaver = None  # type: ignore
    _HAS_SQLITE_SAVER = False

from src.agents.communication_agent import run_communication_agent
from src.agents.consistency_coordinator import run_consistency_coordinator
from src.agents.damage_assessor import run_damage_assessor
from src.agents.fraud_crew import run_fraud_crew
from src.agents.intake_agent import run_intake_agent
from src.agents.policy_checker import run_policy_checker
from src.agents.settlement_calculator import run_settlement_calculator
from src.config import (
    get_agent_config,
    get_confidence_gate_config,
    get_evaluation_config,
    get_hitl_config,
    get_pipeline_config,
)
from src.evaluation.evaluator import run_evaluator
from src.guardrails.manager import guard_node
from src.hitl.checkpoint import check_hitl_required, format_hitl_brief
from src.hitl.queue import enqueue_claim
from src.models.schemas import ClaimDecision, ConflictSeverity, FraudRiskLevel
from src.models.state import ClaimsState, initial_state, ClaimInput
from src.tools.policy_lookup import get_coverage_for_claim_type, lookup_policy
from src.utils import currency_symbol

logger = logging.getLogger(__name__)


# ------- Confidence gates -----------------------------------------------------

def _gate_threshold(agent_key: str) -> float:
    cfg = get_confidence_gate_config()
    return float(cfg.get("per_agent", {}).get(agent_key, cfg.get("default_threshold", 0.60)))


def _check_confidence_gate(agent_key: str, confidence: float | None, estimated_amount: float) -> bool:
    """Return True if confidence is below threshold AND the claim is high-impact (HITL needed).

    Risk-stratified, not confidence-alone: low confidence on a small claim no
    longer pauses the pipeline by itself - a shaky output matters most where
    getting it wrong costs the most. Reuses hitl.triggers.min_amount as the
    single definition of "high-impact" used everywhere else in this system,
    rather than inventing a second amount threshold. Confidence on claims
    below that amount still feeds the HITL priority-score "confidence"
    component if the claim ends up queued for some other reason - it just
    can't add the claim to the queue on its own.
    """
    cfg = get_confidence_gate_config()
    if not cfg.get("enabled", False) or confidence is None:
        return False
    threshold = _gate_threshold(agent_key)
    if confidence >= threshold:
        return False
    min_amount = float(get_hitl_config()["triggers"].get("min_amount", 10000))
    high_impact = estimated_amount >= min_amount
    if not high_impact:
        logger.info(
            f"Confidence gate: {agent_key} confidence {confidence:.2f} < {threshold:.2f}, "
            f"but amount {estimated_amount:,.2f} < {min_amount:,.2f} - not high-impact, no pause"
        )
        return False
    logger.info(
        f"Confidence gate: {agent_key} confidence {confidence:.2f} < {threshold:.2f} "
        f"on high-impact claim ({estimated_amount:,.2f} >= {min_amount:,.2f}) -> HITL"
    )
    return True


# Which agent each confidence-gate HITL node is guarding, and how to read that
# agent's confidence from state. hitl_after_fraud/damage/policy no longer
# exist as separate nodes (folded into route_after_fan_out + hitl_checkpoint
# - see that function's docstring for why), but intake and settlement still
# have their own sequential confidence-gate nodes since they're not part of
# the parallel fan-out.
_GATE_NODES = {
    "hitl_after_intake": ("intake_agent", lambda s: getattr(s.get("intake_output"), "confidence", None)),
    "hitl_after_settlement": ("settlement_calculator", lambda s: getattr(s.get("settlement_output"), "confidence", None)),
}


def _routing_reasons(node_name: str, state: ClaimsState) -> list[str]:
    """Why the router sent this claim to THIS HITL node.

    The router already decided a human is needed. These reasons make that
    decision explicit in the ticket, and they guarantee the node pauses: the
    generic trigger rules in check_hitl_required know nothing about a
    per-agent confidence gate, so on their own they could wave a
    low-confidence claim straight through.
    """
    reasons: list[str] = []
    if node_name in _GATE_NODES:
        agent_key, read_conf = _GATE_NODES[node_name]
        conf = read_conf(state)
        if conf is not None:
            reasons.append(
                f"Confidence gate: {agent_key} confidence {conf:.2f} below {_gate_threshold(agent_key):.2f}"
            )
        else:
            reasons.append(f"Confidence gate: {agent_key} returned no confidence")
        return reasons

    # hitl_checkpoint: guardrail halt, injection detected, failed quality
    # gate, or high fraud score
    if state.get("guardrails_halted"):
        last = (state.get("guardrails_violations") or ["budget or timeout exceeded"])[-1]
        reasons.append(f"Guardrail halt: {last}")
    intake_output = state.get("intake_output")
    if intake_output:
        from src.agents.intake_agent import INJECTION_FLAG_PREFIX
        injection_flags = [f for f in (intake_output.validation_flags or []) if f.startswith(INJECTION_FLAG_PREFIX)]
        for f in injection_flags:
            reasons.append(f"Security: prompt-injection attempt detected at intake - {f[len(INJECTION_FLAG_PREFIX):]}")
    if state.get("evaluation_passed") is False:
        evaluation = state.get("evaluation_output")
        score = getattr(evaluation, "overall_score", None)
        min_score = get_evaluation_config().get("min_score_to_release", 0.70)
        if score is not None:
            reasons.append(f"Evaluator quality gate failed (score {score:.2f} < {min_score:.2f})")
        else:
            reasons.append("Evaluator quality gate failed")
    triggers_cfg = get_hitl_config()["triggers"]
    amount = float(state["claim"].get("estimated_amount", 0) or 0) if state.get("claim") else 0.0
    min_amount = float(triggers_cfg.get("min_amount", 10000))
    if state.get("settlement_output") is not None and amount >= min_amount and not state.get("hitl_required"):
        reasons.append(
            f"High-value claim: {currency_symbol()}{amount:,.0f} >= {currency_symbol()}{min_amount:,.0f} "
            "needs human sign-off on the proposed settlement"
        )
    fraud = state.get("fraud_output")
    review_threshold = float(triggers_cfg.get("fraud_score", 0.45))
    if fraud and fraud.fraud_score >= review_threshold:
        reasons.append(
            f"Fraud score {fraud.fraud_score:.2f} >= review threshold {review_threshold:.2f} "
            f"(risk: {fraud.fraud_risk_level.value})"
        )

    # Damage/policy property-based triggers and all three fan-out branches'
    # confidence gates: previously surfaced by their own hitl_after_* nodes
    # (now removed - see route_after_fan_out's docstring), so this generic
    # path is their only remaining place to appear explicitly in the ticket.
    damage = state.get("damage_output")
    if damage and damage.requires_physical_inspection:
        reasons.append("Damage assessor requires physical inspection before a decision")
    policy = state.get("policy_output")
    if policy and (policy.exclusions_triggered or policy.compliance_flags):
        reasons.append(
            f"Policy exclusions/compliance concerns: "
            f"{', '.join(policy.compliance_flags + policy.exclusions_triggered)}"
        )
    if fraud and _check_confidence_gate("fraud_crew", fraud.confidence, amount):
        reasons.append(f"Confidence gate: fraud_crew confidence {fraud.confidence:.2f} below {_gate_threshold('fraud_crew'):.2f}")
    if damage and _check_confidence_gate("damage_assessor", damage.assessment_confidence, amount):
        reasons.append(f"Confidence gate: damage_assessor confidence {damage.assessment_confidence:.2f} below {_gate_threshold('damage_assessor'):.2f}")
    if policy and _check_confidence_gate("policy_checker", policy.confidence, amount):
        reasons.append(f"Confidence gate: policy_checker confidence {policy.confidence:.2f} below {_gate_threshold('policy_checker'):.2f}")
    return reasons


# ------- HITL Node -------------------------------------------------------------

def hitl_checkpoint_node(state: ClaimsState, config: RunnableConfig) -> dict:
    """
    HITL checkpoint - enqueues the claim for manual review and PAUSES the graph
    via LangGraph's `interrupt()` primitive. The pipeline does not resume until
    an approver calls `resume_claim(...)` with a decision payload.

    One function backs six graph nodes (hitl_checkpoint + five confidence
    gates); it reads its own node name from the run config to know why it was
    called.

    On resume, LangGraph re-runs this function from the first line and
    `interrupt()` returns the reviewer's decision instead of pausing. Every
    side effect before `interrupt()` therefore runs twice per pause. The
    ticket insert is idempotent (keyed on run + node + pause number), so the
    second run returns the ticket the first run created.
    """
    node_name = (config or {}).get("metadata", {}).get("langgraph_node", "hitl_checkpoint")
    claim = state["claim"]
    claim_id = claim["claim_id"]
    fraud_output = state.get("fraud_output")
    damage_output = state.get("damage_output")
    intake_output = state.get("intake_output")
    policy_output = state.get("policy_output")

    logger.info(f"[{claim_id}] HITL checkpoint triggered at {node_name}")

    agent_confidences = [
        c for c in (
            getattr(intake_output, "confidence", None),
            getattr(damage_output, "assessment_confidence", None),
            getattr(policy_output, "confidence", None),
            getattr(state.get("settlement_output"), "confidence", None),
        ) if c is not None
    ]

    _, generic_triggers, priority, priority_score = check_hitl_required(
        claim=dict(claim),
        intake_output=intake_output,
        fraud_output=fraud_output,
        damage_assessed_usd=damage_output.assessed_damage_usd if damage_output else 0,
        agent_confidence_scores=agent_confidences,
        damage_output=damage_output,
        policy_output=policy_output,
    )
    routing_reasons = _routing_reasons(node_name, state)
    # Drop generic triggers that restate a routing reason (both rules report
    # the fraud score, for example); keep the routing reason's wording.
    reason_heads = {" ".join(r.split()[:2]) for r in routing_reasons}
    triggers = routing_reasons + [
        t for t in generic_triggers if " ".join(t.split()[:2]) not in reason_heads
    ]
    if not triggers:
        triggers = [f"Routed to human review by the pipeline at {node_name}"]

    review_brief = format_hitl_brief(
        claim=dict(state.get("masked_claim") or claim),
        triggers=triggers,
        priority=priority,
        fraud_output=fraud_output,
        damage_assessed_usd=damage_output.assessed_damage_usd if damage_output else 0,
    )

    state_snapshot = {
        "claim_id": claim_id,
        "incident_type": claim.get("incident_type"),
        "estimated_amount": claim.get("estimated_amount"),
        "fraud_score": fraud_output.fraud_score if fraud_output else 0,
        "fraud_risk": fraud_output.fraud_risk_level.value if fraud_output else "unknown",
        "assessed_damage": damage_output.assessed_damage_usd if damage_output else 0,
        "ai_settlement": state.get("final_amount_usd", 0),
        "ai_decision": state.get("final_decision").value if state.get("final_decision") else "pending",
        "paused_at": node_name,
        "guardrails_halted": bool(state.get("guardrails_halted")),
    }

    prior_pauses_here = sum(
        1 for entry in (state.get("pipeline_trace") or [])
        if isinstance(entry, dict) and entry.get("agent") == "hitl_checkpoint" and entry.get("node") == node_name
    )
    idempotency_key = f"{claim_id}:{state.get('execution_start_time')}:{node_name}:{prior_pauses_here}"

    ticket_id = enqueue_claim(
        claim_id=claim_id,
        priority=priority,
        priority_score=priority_score,
        triggers=triggers,
        review_brief=review_brief,
        state_snapshot=state_snapshot,
        idempotency_key=idempotency_key,
    )

    logger.info(f"[{claim_id}] Pausing pipeline for manual approval (ticket={ticket_id})")

    # Hard pause. Execution suspends here until an approver supplies a decision
    # via Command(resume={...}) from the /api/hitl/decide endpoint.
    human_result: dict = interrupt({
        "ticket_id": ticket_id,
        "claim_id": claim_id,
        "node": node_name,
        "priority": priority.value,
        "priority_score": priority_score,
        "triggers": triggers,
        "review_brief": review_brief,
        "state_snapshot": state_snapshot,
    })

    decision_str = (human_result or {}).get("decision") or ClaimDecision.ESCALATED_HITL.value
    try:
        decision_enum = ClaimDecision(decision_str)
    except ValueError:
        decision_enum = ClaimDecision.ESCALATED_HITL

    override_amount = (human_result or {}).get("settlement_override_usd")
    result_update = {
        "hitl_required": True,
        "hitl_triggers": triggers,
        "hitl_priority": priority,
        "hitl_priority_score": priority_score,
        "hitl_ticket_id": ticket_id,
        "human_decision": decision_str,
        "human_reviewer_id": (human_result or {}).get("reviewer_id"),
        "human_notes": (human_result or {}).get("notes", ""),
        "human_override": bool((human_result or {}).get("override_ai", False)),
        "final_decision": decision_enum,
        "pipeline_trace": [{
            "agent": "hitl_checkpoint",
            "node": node_name,
            "ticket_id": ticket_id,
            "priority": priority.value,
            "priority_score": priority_score,
            "triggers": triggers,
            "human_decision": decision_str,
            "reviewer_id": (human_result or {}).get("reviewer_id"),
            "decision": decision_str,
            "confidence": None,
            "reasoning": (human_result or {}).get("notes", ""),
            "flags": triggers,
            "findings": {"paused_at": node_name, "priority": priority.value},
        }],
    }
    if override_amount is not None:
        # Kept in its own field as well: if the pipeline continues after this
        # pause, the settlement agent would otherwise overwrite final_amount_usd.
        result_update["final_amount_usd"] = float(override_amount)
        result_update["human_settlement_override_usd"] = float(override_amount)
    logger.info(f"[{claim_id}] Resumed from HITL ({node_name}) with decision={decision_str}")
    return result_update


def auto_reject_node(state: ClaimsState) -> dict:
    """Auto-reject path for confirmed fraud (rules and narrative agree)."""
    claim_id = state["claim"]["claim_id"]
    fraud = state.get("fraud_output")
    logger.warning(f"[{claim_id}] AUTO-REJECT: Confirmed fraud (score={fraud.fraud_score:.2f})")
    return {
        "final_decision": ClaimDecision.AUTO_REJECTED,
        "final_amount_usd": 0.0,
        "pipeline_trace": [{
            "agent": "auto_reject",
            "fraud_score": fraud.fraud_score if fraud else 0,
            "narrative_risk": fraud.narrative_risk if fraud else None,
            "reason": "Confirmed fraud - auto rejected",
            "decision": ClaimDecision.AUTO_REJECTED.value,
            "confidence": fraud.confidence if fraud else None,
            "reasoning": "Composite fraud score and narrative risk crossed the auto-reject line, "
                         "and the claimant's own description admits an intent to deceive.",
            "flags": fraud.primary_concerns if fraud else [],
            "findings": {},
        }],
    }


# ------- Consistency retry prep --------------------------------------------------

def prepare_consistency_retry_node(state: ClaimsState) -> dict:
    """Runs immediately before re-invoking a retry-eligible agent
    (damage_assessor or policy_checker) for an unresolved MATERIAL
    consistency conflict (see route_after_consistency_check). A plain code
    step, not an LLM call: increments this agent's retry count (for the
    cap check) and writes the conflict's description/reasoning into
    consistency_retry_context, which that agent's own prompt-building code
    reads and appends to its normal context (see damage_assessor.py /
    policy_checker.py's own "consistency retry context" sections).

    Only ever acts on the FIRST matching MATERIAL conflict still under its
    retry cap - route_after_consistency_check already picked that same
    conflict when it decided to route here, so this just re-derives it
    rather than passing it through a side channel LangGraph state updates
    don't have a clean way to carry between a router and the node it routes
    to.
    """
    consistency = state.get("consistency_output")
    retry_counts = dict(state.get("consistency_retry_counts") or {})
    max_retries = int(get_agent_config("consistency_coordinator").get("max_retries", 2))

    if not consistency or not consistency.has_conflicts:
        return {}

    for conflict in consistency.conflicts:
        if conflict.severity != ConflictSeverity.MATERIAL or not conflict.retry_target:
            continue
        if retry_counts.get(conflict.retry_target, 0) < max_retries:
            retry_counts[conflict.retry_target] = retry_counts.get(conflict.retry_target, 0) + 1
            context = (
                f"\n        CONSISTENCY CHECK FLAGGED A POTENTIAL CONFLICT (retry "
                f"{retry_counts[conflict.retry_target]}/{max_retries}) - a coordinator who "
                f"reviewed all specialists' findings together found:\n"
                f"        {conflict.description}\n"
                f"        Reasoning: {conflict.reasoning}\n"
                f"        Reconsider your assessment in light of this - if it changes your "
                f"conclusion, say so explicitly in your notes; if it does not, explain why not.\n"
            )
            return {
                "consistency_retry_counts": retry_counts,
                "consistency_retry_context": context,
                "_consistency_retry_target": conflict.retry_target,
            }
    return {}


def route_after_prepare_consistency_retry(state: ClaimsState) -> Literal[
    "damage_assessor", "policy_checker", "consistency_coordinator"
]:
    """Dispatches to whichever agent prepare_consistency_retry_node just
    prepared context for. Falls back to consistency_coordinator (a no-op
    re-check) in the unexpected case where prepare_consistency_retry_node
    found nothing to act on - should not happen in practice since
    route_after_consistency_check only routes here when it already found
    a matching conflict, but this avoids a KeyError rather than assuming
    the field is always present.
    """
    target = state.get("_consistency_retry_target")
    if target in ("damage_assessor", "policy_checker"):
        return target  # type: ignore[return-value]
    return "consistency_coordinator"


# ------- Fan-out join step ------------------------------------------------------

def recompute_covered_amount_node(state: ClaimsState) -> dict:
    """Runs once, after the damage_assessor / policy_checker / fraud_crew
    parallel fan-out has fully joined (see _build_graph_object's fan-out
    edges). policy_checker no longer computes covered_amount_usd itself,
    since it runs concurrently with damage_assessor and has no access to
    the real damage figure at the time it runs (see policy_checker.py's
    module docstring). This is pure arithmetic on numbers now available
    from both branches - not a judgment call, so it's a plain code step,
    not its own agent.

    Mirrors the exact formula policy_checker used to apply internally:
    min(damage - deductible, limit - deductible), floored at 0. A
    NOT_COVERED claim always pays 0 regardless of the damage figure.
    """
    policy = state.get("policy_output")
    damage = state.get("damage_output")
    if not policy:
        return {}

    damage_amount = (
        damage.assessed_damage_usd if damage
        else float(state["claim"].get("estimated_amount", 0))
    )

    from src.models.schemas import CoverageStatus
    if policy.coverage_status == CoverageStatus.NOT_COVERED:
        policy.covered_amount_usd = 0.0
    else:
        coverage = get_coverage_for_claim_type(
            lookup_policy(state["claim"]["policy_number"]) or {}, state["claim"].get("incident_type", "")
        )
        coverage_limit = coverage.get("coverage_limit", 0)
        policy.covered_amount_usd = max(
            0.0, min(damage_amount - policy.deductible_usd, coverage_limit - policy.deductible_usd)
        )
    return {"policy_output": policy}


# ------- Routing Functions ----------------------------------------------------

def route_after_intake(state: ClaimsState) -> list[str] | str:
    """Route after intake: valid claims fan out to three parallel branches
    (damage_assessor, policy_checker, fraud_crew); invalid ones go straight
    to communication. Returning a list routes to all three nodes as one
    LangGraph super-step - they run concurrently, not in sequence (see
    _build_graph_object's fan-out edges and recompute_covered_amount_node,
    the join step that runs once all three complete).

    is_valid is now code-computed (see intake_agent.py), so intake no longer
    has a meaningful confidence signal of its own - IntakeValidationOutput
    has no confidence field. The old confidence-gate call is left in place
    (intake.confidence doesn't exist, so getattr-based callers degrade to
    None and skip the gate) rather than deleted, per an explicit decision to
    keep the hitl_after_intake node wired but inert.

    Prompt-injection detection (src/security/injection_check.py) is intake's
    replacement early pause point: property-based, not confidence-based, and
    applies regardless of claim amount - an injection attempt is a security
    concern, not a routine judgment call (see CLAUDE.md "Prompt injection
    defense"). It also must not route on a claim already headed to denial.
    """
    if state.get("guardrails_halted"):
        return "hitl_checkpoint"

    intake = state.get("intake_output")

    if not intake or not intake.is_valid:
        logger.info("Routing to denial: intake invalid")
        return "communication_agent"

    from src.agents.intake_agent import INJECTION_FLAG_PREFIX
    if any(f.startswith(INJECTION_FLAG_PREFIX) for f in (intake.validation_flags or [])):
        logger.warning("Routing to mandatory human review: injection attempt detected at intake")
        return "hitl_checkpoint"

    # The claimant kept their own incident_type after /verify-type suggested
    # a different one (src/security/incident_type_check.py). Neither the
    # claimant's choice nor the model's suggestion should silently win on an
    # unresolved disagreement - a human sees both sides (the model's
    # reasoning and the claimant's own stated justification, both already
    # surfaced to the claimant before they chose to keep their selection).
    # Property-based, like the injection check above - applies regardless
    # of claim amount.
    if state["claim"].get("type_disputed_by_claimant"):
        logger.warning("Routing to mandatory human review: claimant disputed the suggested incident_type")
        return "hitl_checkpoint"

    # Confidence gate (risk-stratified: only on high-impact claims). Dead for
    # intake specifically now (no confidence field), kept for the other
    # nodes that share this helper via _GATE_NODES.
    estimated_amount = float(state["claim"].get("estimated_amount", 0))
    if _check_confidence_gate("intake_agent", getattr(intake, "confidence", None), estimated_amount):
        return "hitl_after_intake"

    # Fast mode: small claims skip the fan-out entirely
    pipeline_cfg = get_pipeline_config()
    fast_mode = pipeline_cfg.get("fast_mode", {})
    if (
        fast_mode.get("enabled", False)
        and float(state["claim"].get("estimated_amount", 0)) < fast_mode.get("max_amount", 500)
    ):
        logger.info("Fast mode: routing directly to settlement")
        return "settlement_calculator"

    # Fan-out: damage_assessor, policy_checker, fraud_crew all run
    # concurrently. None of the three needs another's output (confirmed by
    # reading each agent's actual state reads, not assumed) - the one real
    # cross-dependency, policy's covered_amount_usd needing the real damage
    # figure, is handled by recompute_covered_amount_node AFTER the join,
    # not by making policy_checker wait on damage_assessor.
    return ["damage_assessor", "policy_checker", "fraud_crew"]


def fan_out_join_node(state: ClaimsState) -> dict:
    """No-op pass-through: the real, single join point for the
    damage_assessor / policy_checker / fraud_crew parallel fan-out.

    Real bug found and fixed here (caught by a live mocked-graph E2E test,
    not by code review or unit tests of route_after_fan_out in isolation):
    route_after_fan_out used to be wired as its own add_conditional_edges
    call on EACH of the three branch nodes directly (three separate calls:
    add_conditional_edges("damage_assessor", route_after_fan_out, ...),
    same for "policy_checker" and "fraud_crew"). LangGraph evaluates a
    conditional edge immediately after ITS OWN source node completes, not
    after a shared join - so each of those three calls ran against that
    ONE branch's own partial state, not the fully-merged post-fan-out
    state the comment above this function used to claim ("evaluated once
    all three branches have completed"). For a CONFIRMED-fraud claim,
    fraud_crew's own call correctly saw fraud_output and returned
    "auto_reject" - but damage_assessor's and policy_checker's calls ran
    before fraud_output existed in THEIR view of state, so they returned
    "consistency_coordinator" instead. LangGraph doesn't know these three
    routing decisions were meant to be the same decision; it just sees
    three independent edges and executes the union of their distinct
    targets - so the claim went through BOTH auto_reject AND
    consistency_coordinator in the same run, and the consistency path's
    own downstream checks then fired a spurious extra HITL pause on a
    claim that should have gone straight to auto-reject.

    Fix: all three branches now point here via a plain (unconditional)
    edge - a genuine fan-in, which LangGraph only executes once all three
    writes are merged (this part of the original claim WAS correct - a
    single shared target node, reached from multiple sources, is a real
    join). route_after_fan_out's conditional edge is attached to THIS node
    alone, so it runs exactly once, against the one true merged state.
    """
    return {}


def route_after_fan_out(state: ClaimsState) -> Literal[
    "consistency_coordinator", "auto_reject", "hitl_checkpoint"
]:
    """Router evaluated from fan_out_join_node - the single real join point
    after all three fan-out branches (damage_assessor, policy_checker,
    fraud_crew) have completed (see that function's docstring for why this
    must be a dedicated join node, not three separate conditional edges).
    Handles only the two conditions that should pre-empt the consistency
    check entirely (no point reading three findings together for mutual
    consistency if the claim is already being auto-rejected or the
    pipeline already halted) - everything else (fraud/damage/policy
    triggers, confidence gates) is checked AFTER the consistency
    coordinator runs, in route_after_consistency_check, since a MATERIAL
    conflict there can change damage_output/policy_output via a retry
    before those checks should run.

    LangGraph does not support a clean per-branch interrupt mid-fan-out
    (confirmed empirically against this project's installed LangGraph
    1.2.16: a sibling branch's own interrupt() pauses the whole
    super-step, but branches that don't interrupt still finish and their
    writes ARE visible in the paused state), so every pause/reject
    condition from all three branches is still checked together, just
    split across two router functions now instead of one, to make room
    for the consistency check and its retry loop in between.
    """
    if state.get("guardrails_halted"):
        return "hitl_checkpoint"

    fraud = state.get("fraud_output")
    if fraud and fraud.fraud_risk_level == FraudRiskLevel.CONFIRMED:
        return "auto_reject"

    return "consistency_coordinator"


def route_after_consistency_check(state: ClaimsState) -> Literal[
    "prepare_consistency_retry", "settlement_calculator", "hitl_checkpoint"
]:
    """Second-stage router: handles the consistency coordinator's retry
    loop, then falls through to the same property-based triggers and
    confidence gates route_after_fan_out used to check directly (fraud
    HITL threshold, damage inspection, policy exclusions/compliance,
    confidence gates) - now evaluated AFTER any retry has had a chance to
    update damage_output/policy_output, so a resolved conflict doesn't
    stale-trigger a check that a retry already addressed.

    Retry loop: a MATERIAL conflict routes back to its retry_target agent
    (damage_assessor for SEVERITY_MISMATCH, policy_checker for
    MISSED_FACT - fraud_crew is never a retry target, per explicit scope
    decision) WITH THE CONFLICT ADDED AS CONTEXT (see
    _consistency_retry_context), up to agents.consistency_coordinator.
    max_retries (default 2) retries PER AGENT - "always bound the loop,
    define what happens when it exhausts attempts" (CLAUDE.md evaluator-
    optimizer guidance). Once the cap is reached for an agent, or no
    MATERIAL conflict remains unresolved, proceeds to the normal
    trigger/confidence-gate checks - an unresolved MATERIAL conflict at
    that point becomes its own named HITL trigger (see
    src/hitl/checkpoint.py), never silently dropped.
    """
    if state.get("guardrails_halted"):
        return "hitl_checkpoint"

    consistency = state.get("consistency_output")
    retry_counts = dict(state.get("consistency_retry_counts") or {})
    max_retries = int(get_agent_config("consistency_coordinator").get("max_retries", 2))

    if consistency and consistency.has_conflicts:
        for conflict in consistency.conflicts:
            if conflict.severity != ConflictSeverity.MATERIAL or not conflict.retry_target:
                continue
            already_retried = retry_counts.get(conflict.retry_target, 0)
            if already_retried < max_retries:
                logger.warning(
                    f"[{state['claim']['claim_id']}] Consistency conflict ({conflict.conflict_type.value}) "
                    f"routing back to {conflict.retry_target} (retry {already_retried + 1}/{max_retries})"
                )
                return "prepare_consistency_retry"
            logger.warning(
                f"[{state['claim']['claim_id']}] Consistency conflict ({conflict.conflict_type.value}) "
                f"unresolved after {max_retries} retries of {conflict.retry_target} - routing to HITL"
            )
            return "hitl_checkpoint"

    fraud = state.get("fraud_output")
    review_threshold = float(get_hitl_config()["triggers"].get("fraud_score", 0.45))
    if fraud and fraud.fraud_score >= review_threshold:
        return "hitl_checkpoint"

    damage = state.get("damage_output")
    if damage and damage.requires_physical_inspection:
        return "hitl_checkpoint"

    policy = state.get("policy_output")
    if policy and (policy.exclusions_triggered or policy.compliance_flags):
        return "hitl_checkpoint"

    estimated_amount = float(state["claim"].get("estimated_amount", 0))
    if fraud and _check_confidence_gate("fraud_crew", fraud.confidence, estimated_amount):
        return "hitl_checkpoint"
    if damage and _check_confidence_gate("damage_assessor", damage.assessment_confidence, estimated_amount):
        return "hitl_checkpoint"
    if policy and _check_confidence_gate("policy_checker", policy.confidence, estimated_amount):
        return "hitl_checkpoint"

    return "settlement_calculator"


def route_after_settlement(state: ClaimsState) -> Literal[
    "evaluator", "hitl_after_settlement", "hitl_checkpoint"
]:
    """Confidence gate after settlement calculation (risk-stratified: only on high-impact claims)."""
    if state.get("guardrails_halted"):
        return "hitl_checkpoint"
    settlement = state.get("settlement_output")
    estimated_amount = float(state["claim"].get("estimated_amount", 0))
    if settlement and _check_confidence_gate("settlement_calculator", settlement.confidence, estimated_amount):
        return "hitl_after_settlement"
    return "evaluator"


def route_after_evaluation(state: ClaimsState) -> Literal[
    "hitl_checkpoint", "communication_agent"
]:
    """If evaluation FAILED the quality gate, route to HITL before release.

    Three states, not two: True (judged, passed), False (judged, failed), None
    (never judged -- the sampler skipped it). Only an explicit False diverts to
    a human; an unevaluated claim continues, which is what sampling is for.
    Writing this as `if not evaluation_passed` would send every skipped claim to
    HITL and defeat the sampling entirely.
    """
    if state.get("guardrails_halted"):
        return "hitl_checkpoint"
    evaluation_passed = state.get("evaluation_passed")
    if evaluation_passed is False:
        logger.info("Evaluation failed quality gate - routing to HITL")
        return "hitl_checkpoint"
    if _high_value_needs_signoff(state):
        logger.info("High-value claim - routing to HITL for settlement sign-off")
        return "hitl_checkpoint"
    if evaluation_passed is None:
        logger.info("Claim was not evaluated (sampled out) - continuing unjudged")
    return "communication_agent"


_CONTINUE_DECISIONS = {ClaimDecision.APPROVED.value, ClaimDecision.APPROVED_PARTIAL.value}


def _high_value_needs_signoff(state: ClaimsState) -> bool:
    """High-value claims (hitl.triggers.min_amount, per country) need a human
    to sign off on the proposed settlement, unless a human already reviewed
    this claim earlier in the run."""
    if state.get("hitl_required"):
        return False
    min_amount = float(get_hitl_config()["triggers"].get("min_amount", 10000))
    return float((state.get("claim") or {}).get("estimated_amount", 0) or 0) >= min_amount


def route_after_hitl_checkpoint(state: ClaimsState) -> list[str] | str:
    """Route after hitl_checkpoint resume.

    - Guardrail halt: the human's decision stands; no more agent spend.
    - Reviewer did not approve (denied, fraud investigation, pending documents,
      escalated): nothing left to calculate, write the letter.
    - A pause from the fan-out (fraud/damage/policy trigger, confidence
      gate, or an unresolved consistency conflict at its retry cap - see
      route_after_fan_out / route_after_consistency_check) that the
      reviewer approved: all three branches already ran (confirmed
      empirically - a sibling branch's own interrupt() pauses the whole
      super-step, but branches that don't interrupt still finish and their
      writes are visible in the paused state), so there's nothing left to
      fan out again. A human approving past an unresolved consistency
      conflict means proceed with it as-is (the conflict stays on the
      record via the trace, but the human has the final say, same
      "human approval is respected" symmetry as every other HITL path in
      this pipeline) - continue straight to the covered-amount join step
      and settlement, never back into another retry loop.
    - A pause BEFORE the fan-out (injection detected, disputed incident
      type - both fire in route_after_intake, which runs before the fan-out
      exists) that the reviewer approved: damage/policy/fraud haven't run
      at all yet (damage_output is None), so resume by launching the
      fan-out now, same three parallel branches route_after_intake would
      have started.
    - Eval-triggered pause: everything already ran including settlement, go
      to communication.
    """
    if state.get("guardrails_halted"):
        logger.info("HITL resume after guardrail halt -> communication_agent")
        return "communication_agent"
    decision = state.get("human_decision")
    if decision is not None and decision not in _CONTINUE_DECISIONS:
        logger.info(f"HITL resume: reviewer decided {decision} -> communication_agent")
        return "communication_agent"
    if state.get("settlement_output") is not None:
        logger.info("HITL resume: eval-triggered, continuing to communication_agent")
        return "communication_agent"
    if state.get("damage_output") is None:
        logger.info("HITL resume: pre-fan-out pause (injection/type dispute), launching the fan-out")
        return ["damage_assessor", "policy_checker", "fraud_crew"]
    logger.info("HITL resume: fan-out pause approved, continuing to settlement join")
    return "recompute_covered_amount"


# ------- Checkpointer (shared across invocations so interrupt/resume works) ---

_CHECKPOINT_DB = str(Path(__file__).resolve().parent.parent / "data" / "claims_checkpoints.db")
_checkpointer_ctx = None   # must stay alive so the SQLite connection isn't garbage-collected
_checkpointer = None
_compiled_graph = None


def _get_checkpointer():
    """Lazy, process-lifetime checkpointer so interrupted claims can resume.

    Prefers SqliteSaver (durable across restarts). Falls back to MemorySaver
    if langgraph-checkpoint-sqlite isn't installed, so the pipeline still runs.
    """
    global _checkpointer, _checkpointer_ctx
    if _checkpointer is not None:
        return _checkpointer

    if not _HAS_SQLITE_SAVER:
        logger.warning(
            "langgraph-checkpoint-sqlite not installed - falling back to "
            "MemorySaver. Paused (HITL) claims will NOT survive a server "
            "restart. Run: uv pip install langgraph-checkpoint-sqlite"
        )
        _checkpointer = MemorySaver()
        return _checkpointer

    Path(_CHECKPOINT_DB).parent.mkdir(parents=True, exist_ok=True)
    _checkpointer_ctx = SqliteSaver.from_conn_string(_CHECKPOINT_DB)
    _checkpointer = _checkpointer_ctx.__enter__()
    return _checkpointer


def get_compiled_graph():
    """Return the process-wide compiled graph (built once, cached)."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = _build_graph_object().compile(checkpointer=_get_checkpointer())
    return _compiled_graph


def _build_graph_object() -> StateGraph:
    """
    Build and return the LangGraph StateGraph (compiled by get_compiled_graph).

    Every agent node runs inside guard_node(), which enforces the per-claim
    budget before the agent and records tokens, cost and time after it.

    START
      └─ intake_agent
           ├─ [invalid] -------------------------------------------------> communication_agent
           ├─ [low_confidence + high_impact] -------------------------> hitl_after_intake -> fan-out
           ├─ [fast_mode] -----------------------------------------------> settlement_calculator
           └─ [fan-out: all three run concurrently, none needs another's output]
                ├─ damage_assessor  ─┐
                ├─ policy_checker   ─┤  (join: all three complete)
                └─ fraud_crew       ─┘
                       │
                       ▼
                route_after_fan_out (one combined check, replaces the old
                per-agent sequential routers - see its docstring for why:
                LangGraph pauses the whole super-step on any one branch's
                interrupt(), so there's no clean way to pause only one
                branch while the others keep running - every trigger from
                all three branches is therefore checked together, once,
                after all three have already finished)
                       │
                ├─ [confirmed_fraud] ------------------------------------> auto_reject -> communication_agent
                ├─ [any fan-out trigger/confidence gate] ----------------> hitl_checkpoint
                └─ recompute_covered_amount (code-only join step: now that
                   damage_output exists, compute policy_output's real
                   covered_amount_usd - policy_checker itself can't do this,
                   since it runs concurrently with damage and has no access
                   to the real damage figure)
                     └─ settlement_calculator
                          ├─ [low_confidence + high_impact] -------------> hitl_after_settlement -> evaluator
                          └─ evaluator
                               ├─ [passed / not sampled] -------------------> communication_agent
                               └─ [failed] -------------------------------> hitl_checkpoint

    hitl_checkpoint resumes to the fan-out (pre-fan-out pause: injection/type
    dispute), to recompute_covered_amount (fan-out pause approved - all
    three branches already ran), or to communication_agent (everything
    else). Any router sends a guardrail-halted claim to hitl_checkpoint.
    """
    graph = StateGraph(ClaimsState)

    # ------- Core agent nodes (each wrapped in the guardrails) ---------------------
    graph.add_node("intake_agent", guard_node("intake_agent", run_intake_agent))
    graph.add_node("fraud_crew", guard_node("fraud_crew", run_fraud_crew))
    graph.add_node("damage_assessor", guard_node("damage_assessor", run_damage_assessor))
    graph.add_node("policy_checker", guard_node("policy_checker", run_policy_checker))
    graph.add_node("fan_out_join", fan_out_join_node)
    graph.add_node("consistency_coordinator", guard_node("consistency_coordinator", run_consistency_coordinator))
    graph.add_node("prepare_consistency_retry", prepare_consistency_retry_node)
    graph.add_node("recompute_covered_amount", recompute_covered_amount_node)
    graph.add_node("settlement_calculator", guard_node("settlement_calculator", run_settlement_calculator))
    graph.add_node("evaluator", guard_node("evaluator", run_evaluator))
    graph.add_node("auto_reject", auto_reject_node)
    graph.add_node(
        "communication_agent",
        guard_node("communication_agent", run_communication_agent, enforce_budget=False),
    )

    # ------- HITL nodes (same function, different node names -> different resume targets)
    graph.add_node("hitl_checkpoint", hitl_checkpoint_node)       # fan-out trigger / eval / guardrail halt
    graph.add_node("hitl_after_intake", hitl_checkpoint_node)     # intake low confidence
    graph.add_node("hitl_after_settlement", hitl_checkpoint_node) # settlement low confidence

    # ------- Entry: fan out to three parallel branches -------------------------------
    graph.add_edge(START, "intake_agent")

    graph.add_conditional_edges("intake_agent", route_after_intake, {
        "damage_assessor": "damage_assessor",
        "policy_checker": "policy_checker",
        "fraud_crew": "fraud_crew",
        "communication_agent": "communication_agent",
        "settlement_calculator": "settlement_calculator",
        "hitl_after_intake": "hitl_after_intake",
        "hitl_checkpoint": "hitl_checkpoint",
    })

    # ------- Join: all three fan-out branches converge on fan_out_join -------------
    # A genuine LangGraph fan-in: three plain edges into ONE node, which
    # LangGraph only executes once, after all three writes are merged.
    # route_after_fan_out's conditional edge is attached to that join node
    # alone (not to each branch separately - see fan_out_join_node's
    # docstring for the real bug this fixes: three separate conditional
    # edges on the three branches each ran against that branch's own
    # partial state, not the merged post-fan-out state).
    #
    # Also the path a retry's completion (damage_assessor/policy_checker
    # re-run with new context) rejoins through - same join node, same
    # fresh route_after_fan_out check, since both guardrail-halted and
    # fraud-CONFIRMED are still worth checking fresh either way.
    graph.add_edge("damage_assessor", "fan_out_join")
    graph.add_edge("policy_checker", "fan_out_join")
    graph.add_edge("fraud_crew", "fan_out_join")
    graph.add_conditional_edges("fan_out_join", route_after_fan_out, {
        "consistency_coordinator": "consistency_coordinator",
        "auto_reject": "auto_reject",
        "hitl_checkpoint": "hitl_checkpoint",
    })

    # ------- Consistency check: resolve retries before settlement -------------------
    graph.add_conditional_edges("consistency_coordinator", route_after_consistency_check, {
        "prepare_consistency_retry": "prepare_consistency_retry",
        "settlement_calculator": "recompute_covered_amount",
        "hitl_checkpoint": "hitl_checkpoint",
    })
    graph.add_conditional_edges("prepare_consistency_retry", route_after_prepare_consistency_retry, {
        "damage_assessor": "damage_assessor",
        "policy_checker": "policy_checker",
        "consistency_coordinator": "consistency_coordinator",
    })

    graph.add_edge("recompute_covered_amount", "settlement_calculator")

    graph.add_conditional_edges("settlement_calculator", route_after_settlement, {
        "evaluator": "evaluator",
        "hitl_after_settlement": "hitl_after_settlement",
        "hitl_checkpoint": "hitl_checkpoint",
    })

    graph.add_conditional_edges("evaluator", route_after_evaluation, {
        "hitl_checkpoint": "hitl_checkpoint",
        "communication_agent": "communication_agent",
    })

    # ------- HITL resume targets -----------------------------------------------------
    graph.add_conditional_edges("hitl_after_intake", lambda s: ["damage_assessor", "policy_checker", "fraud_crew"], {
        "damage_assessor": "damage_assessor", "policy_checker": "policy_checker", "fraud_crew": "fraud_crew",
    })
    graph.add_edge("hitl_after_settlement", "evaluator")
    graph.add_conditional_edges("hitl_checkpoint", route_after_hitl_checkpoint, {
        "damage_assessor": "damage_assessor",
        "policy_checker": "policy_checker",
        "fraud_crew": "fraud_crew",
        "recompute_covered_amount": "recompute_covered_amount",
        "communication_agent": "communication_agent",
    })

    # ------- Terminal edges ---------------------------------------------------------
    graph.add_edge("auto_reject", "communication_agent")
    graph.add_edge("communication_agent", END)
    return graph


# Backwards-compat alias (other code/tests may still import this).
def build_claims_graph():
    return get_compiled_graph()


# ------- Public Entry Points --------------------------------------------------

def _thread_config(claim_id: str) -> dict:
    return {"configurable": {"thread_id": claim_id}}


def _is_paused(final_state: dict) -> bool:
    """A paused graph surfaces __interrupt__ in the last state."""
    return bool(final_state.get("__interrupt__"))


def _interrupt_payload(final_state: dict) -> dict:
    interrupts = final_state.get("__interrupt__") or []
    return interrupts[0].value if interrupts else {}


def _store_to_memory(final_state: dict) -> None:
    """After pipeline completion, persist the claim to long-term + episodic memory.

    This is what makes the system learn from its own decisions over time.
    Future claims retrieve these as "similar past claims" via the memory
    tools, giving agents historical context for better decisions.

    Everything written here is PII-masked first. Memory outlives the claim and
    is read back into future prompts, so an unmasked description would leak
    one claimant's details into another claimant's LLM call.
    """
    try:
        from src.memory.manager import memory
        from src.security.pii_masker import mask_claim, mask_text

        claim = final_state.get("claim", {})
        claim_id = claim.get("claim_id", "unknown")
        masked = final_state.get("masked_claim") or mask_claim(dict(claim))
        description = masked.get("incident_description", "")
        decision = final_state.get("final_decision")
        decision_str = decision.value if hasattr(decision, "value") else str(decision or "unknown")

        fraud_output = final_state.get("fraud_output")
        fraud_score = float(getattr(fraud_output, "fraud_score", 0)) if fraud_output else None

        # Long-term memory - store every completed claim
        memory.store_claim_outcome(
            claim_id=claim_id,
            description=description,
            metadata={
                "incident_type": claim.get("incident_type", ""),
                "estimated_amount": claim.get("estimated_amount", 0),
                "decision": decision_str,
                "settlement_amount": final_state.get("final_amount_usd", 0),
                "fraud_score": fraud_score,
                "hitl_required": final_state.get("hitl_required", False),
                "human_override": final_state.get("human_override", False),
            },
        )

        # Episodic memory - store notable events
        if final_state.get("human_override"):
            memory.store_episode(
                claim_id=claim_id,
                narrative=(
                    f"Claim {claim_id} ({claim.get('incident_type', '?')}): "
                    f"AI recommended {decision_str} but reviewer overrode to "
                    f"{final_state.get('human_decision', '?')}. "
                    f"Notes: {mask_text(final_state.get('human_notes') or 'none')}"
                ),
                event_type="human_override",
                metadata={"fraud_score": fraud_score, "reviewer": final_state.get("human_reviewer_id")},
            )

        if decision_str == ClaimDecision.AUTO_REJECTED.value:
            memory.store_episode(
                claim_id=claim_id,
                narrative=(
                    f"Claim {claim_id} auto-rejected. Fraud score: {(fraud_score or 0):.2f}. "
                    f"Description: {description[:200]}"
                ),
                event_type="fraud_confirmed",
                metadata={"fraud_score": fraud_score},
            )

        eval_output = final_state.get("evaluation_output")
        if eval_output and not getattr(eval_output, "passed", True):
            memory.store_episode(
                claim_id=claim_id,
                narrative=(
                    f"Claim {claim_id} failed quality gate. Eval score: "
                    f"{getattr(eval_output, 'overall_score', '?')}. "
                    f"Routed to HITL for human review."
                ),
                event_type="quality_gate_failed",
                metadata={"eval_score": getattr(eval_output, "overall_score", None)},
            )

        logger.debug("[%s] Stored in long-term + episodic memory", claim_id)
    except Exception as e:
        # Memory storage should never crash the pipeline
        logger.warning("Memory storage skipped: %s", e)


def process_claim(claim_input: ClaimInput, documents: Optional[dict] = None) -> dict:
    """
    Run the pipeline. Returns one of:
      { "paused": True,  "state": {...}, "interrupt": {...} }   # awaiting approver
      { "paused": False, "state": {...} }                        # completed
    Callers MUST check "paused" and, if True, persist status=pending_human_review.

    Token, cost and processing-time totals are carried in the graph state by
    the guardrails wrapper, so they are correct for the whole claim even
    across a HITL pause.

    documents: parsed AutoCollisionDocuments content (see src/models/
    documents.py), passed straight through to initial_state(). Optional and
    None by default - matches initial_state's own existing signature; this
    was a real, previously-undiscovered gap (found 2026-10-20 while
    live-testing data/test_scenarios): NOTHING in the project actually
    called process_claim() with real document content before this, not
    even evaluation/run_eval.py against the stratified dataset that was
    specifically generated WITH real *_documents.json content - so
    claim_documents was always None on every real run through this
    function, forcing damage_assessor's requires_physical_inspection=True
    hard rule on every claim with no attached repair-estimate document,
    regardless of whether a caller actually had real document content to
    offer. The real API (api/routes_claims.py) still has no real document
    upload capability (separate, already-documented gap - see "Document
    extraction" elsewhere in this file) and is unaffected by this change,
    since it simply never passes this new argument, same as before.
    """
    from src.llm import reset_token_tracking, get_token_usage

    graph = get_compiled_graph()
    claim_id = claim_input["claim_id"]
    state = initial_state(claim_input, documents)
    state["execution_start_time"] = datetime.now(timezone.utc).isoformat()
    reset_token_tracking()

    logger.info(f"[{claim_id}] Pipeline starting")
    cfg = _thread_config(claim_id)

    try:
        final_state = graph.invoke(state, config=cfg)
    except Exception as e:
        logger.error(f"[{claim_id}] Pipeline crashed: {e}", exc_info=True)
        state["error_log"] = [f"Pipeline crash: {str(e)}"]
        state["final_decision"] = ClaimDecision.ESCALATED_HITL
        usage = get_token_usage()
        state["total_tokens_used"] = usage["total"]
        state["total_cost_usd"] = usage["cost"]
        return {"paused": False, "state": state}

    if _is_paused(final_state):
        logger.info(f"[{claim_id}] Pipeline paused at HITL checkpoint")
        return {"paused": True, "state": final_state, "interrupt": _interrupt_payload(final_state)}

    cs = currency_symbol()
    logger.info(
        f"[{claim_id}] Pipeline complete: decision={final_state.get('final_decision')}, "
        f"amount={cs}{(final_state.get('final_amount_usd') or 0):,.2f}, "
        f"tokens={final_state.get('total_tokens_used', 0)}, "
        f"cost=${(final_state.get('total_cost_usd') or 0):.6f}"
    )
    _store_to_memory(final_state)
    return {"paused": False, "state": final_state}


def resume_claim(claim_id: str, decision: dict) -> dict:
    """
    Resume a paused pipeline with an approver's decision.

    decision = {
        "decision": "approved" | "denied" | "escalated_human_review" | ...,
        "reviewer_id": str,
        "notes": str,
        "override_ai": bool,
        "settlement_override_usd": float | None,
    }
    """
    from src.llm import reset_token_tracking

    graph = get_compiled_graph()
    cfg = _thread_config(claim_id)
    reset_token_tracking()
    logger.info(f"[{claim_id}] Resuming pipeline with decision={decision.get('decision')}")
    final_state = graph.invoke(Command(resume=decision), config=cfg)

    if _is_paused(final_state):
        # Legitimate: e.g. fraud HITL approved, then the evaluator quality gate
        # fails -> a second pause on the same thread, with a NEW ticket.
        logger.info(f"[{claim_id}] Pipeline paused again after resume (e.g. eval quality gate)")
        return {"paused": True, "state": final_state, "interrupt": _interrupt_payload(final_state)}

    logger.info(f"[{claim_id}] Pipeline completed after human approval")
    _store_to_memory(final_state)
    return {"paused": False, "state": final_state}
