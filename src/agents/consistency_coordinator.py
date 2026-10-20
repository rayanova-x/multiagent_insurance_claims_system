"""
Consistency Coordinator (LangGraph Node)

Runs once, after the damage_assessor / policy_checker / fraud_crew parallel
fan-out joins (see src/agents/graph.py) - a single isolated LLM call that
reads all three branches' already-finished outputs TOGETHER and checks for
two specific, named cross-branch conflict types (ConflictType in
src/models/schemas.py). This is genuine "independent verification with a
fresh context" (one of CLAUDE.md's three valid multi-agent justifications):
each branch runs fully isolated from the others by design, so a
contradiction between two branches' findings is structurally invisible to
either branch on its own - it can only be seen once both are placed side by
side.

WHY ONLY TWO CONFLICT TYPES, NOT A GENERAL "DO THESE SEEM INCONSISTENT"
CHECK: a third, explicitly considered candidate - whether damage's repair
line items plausibly match the described incident severity - was dropped
after confirming damage_assessor already receives BOTH the incident
description and the repair line items in its own single prompt call, and
its own Rule 3 (40% deviation cap, src/agents/damage_assessor.py) already
acts on exactly that comparison. Checking it again here would be re-grading
a question damage_assessor already asked and answered by hard rule, not
seeing anything genuinely new - the same "schema validity is not
correctness" redundancy already removed from the evaluator (see
src/evaluation/evaluator.py's own docstring for that precedent). Open-ended
"do these seem inconsistent" judgment was also deliberately rejected in
favor of two explicit, named types with real-world grounding (checked
against actual adjuster/fraud-investigation practice, not invented) -
same discipline as every other prompt criteria in this codebase
(src/security/injection_check.py, src/agents/policy_checker.py's
per-exclusion criteria).

This node NEVER re-adjudicates a branch's own judgment call (e.g. it does
not second-guess whether policy_checker applied an exclusion criterion
correctly) - only whether the branches' findings are mutually consistent.
Whether to actually RETRY a flagged agent is decided by CODE in graph.py
from the findings' severity and the configured retry cap - this agent's
job is narrower: read, compare, judge severity, explain. Same "code
decides routing, LLM only judges" split used by every other agent in this
pipeline.
"""

from __future__ import annotations

import logging
import time

from langchain_core.messages import HumanMessage, SystemMessage

from src.llm import get_structured_llm
from src.models.schemas import ConsistencyCheckOutput
from src.models.state import ClaimsState
from src.security.audit_log import log_agent_action
from src.utils import currency_symbol as _sym

logger = logging.getLogger(__name__)
AGENT_NAME = "consistency_coordinator"

SYSTEM_PROMPT = """You are a senior insurance claims coordinator. Three
specialists have each independently assessed one aspect of this claim, in
complete isolation from each other - none of them saw the others' findings.
Your ONLY job is to read all three specialists' findings together and check
for two SPECIFIC, NAMED kinds of contradiction between them. You are NOT
re-adjudicating any specialist's own judgment call - only whether their
findings are mutually consistent with each other.

CONFLICT TYPE 1 - MISSED_FACT: did the Damage Assessor's notes or itemized
repair line items surface a specific, concrete fact that the Policy
Checker's exclusion judgment never had a chance to weigh, because policy
review runs independently of damage review and never sees damage's
findings? Examples of facts that matter: evidence of commercial/delivery
use (fleet markings, commercial equipment, delivery company branding),
evidence the vehicle was used for racing (racing modifications, safety
cage, slick tires), evidence of pre-existing damage inconsistent with a
single new event. Do NOT flag a missed fact that is vague or speculative -
require something CONCRETE stated in the damage assessor's own notes or
line items, naming the specific words/items that support it.

CONFLICT TYPE 2 - SEVERITY_MISMATCH: does the Damage Assessor's severity
conclusion (repair vs. total_loss, and the assessed dollar amount) actually
contradict what the Fraud Crew's narrative-consistency review concluded
about the claim's described severity? A real example: Damage Assessor
concludes total_loss (the vehicle is destroyed), but the Fraud Crew's
narrative summary describes the incident as "minor damage" or "small dent"
with nothing in the story suggesting a total-loss-level event. Do NOT flag
ordinary disagreement about tone or minor wording - require the two
specialists' own stated conclusions to actually point in different
directions about how severe this incident was.

SEVERITY ASSIGNMENT - tied to real consequence, not how different the texts
sound:
- MATERIAL: the conflict could plausibly change the payout amount, the
  coverage decision, or the fraud classification if it were resolved
  differently. Only a MATERIAL finding should ever be retried.
- NOTABLE: the conflict affects explanation quality or confidence in the
  record, but would not change the actual decision even if resolved
  differently. Log it, do not request a retry over it.

If you are not confident a finding meets the bar above, do not report it -
false positives waste a real specialist's time on a second pass. Report
has_conflicts=false and an empty conflicts list when nothing meets the bar."""


def run_consistency_coordinator(state: ClaimsState) -> dict:
    """LangGraph node for the post-fan-out consistency check."""
    claim = state["claim"]
    claim_id = claim["claim_id"]
    damage = state.get("damage_output")
    policy = state.get("policy_output")
    fraud = state.get("fraud_output")
    start_time = time.time()

    logger.info(f"[{claim_id}] Consistency coordinator started")

    llm = get_structured_llm(ConsistencyCheckOutput)
    _s = _sym()

    damage_summary = "Not available"
    if damage:
        line_items_text = "\n".join(
            f"      - {item.get('description', 'item')}: {_s}{item.get('amount', 0):,.2f}"
            for item in (damage.line_items or [])
        ) or "      (none itemized)"
        damage_summary = f"""
    Conclusion: {damage.repair_vs_replace}
    Assessed Amount: {_s}{damage.assessed_damage_usd:,.2f}
    Requires Physical Inspection: {damage.requires_physical_inspection}
    Notes: {damage.assessment_notes}
    Line Items:
{line_items_text}
"""

    policy_summary = "Not available"
    if policy:
        policy_summary = f"""
    Coverage Status: {policy.coverage_status.value}
    Exclusions Triggered: {', '.join(policy.exclusions_triggered) or 'None'}
    Notes: {policy.coverage_notes}
"""

    fraud_summary = "Not available"
    if fraud:
        fraud_summary = f"""
    Fraud Risk: {fraud.fraud_risk_level.value} (score {fraud.fraud_score:.2f})
    Narrative/Story Summary: {fraud.crew_summary}
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

    try:
        output = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
    except Exception as e:
        logger.error(f"[{claim_id}] Consistency coordinator LLM failed: {e}")
        # Fail toward caution but not toward blocking: an unreadable check
        # should not itself force a retry loop (no finding to act on), but
        # must not be silently invisible either - same fail-toward-caution
        # shape as src/security/injection_check.py, scoped to "surface it"
        # rather than "assume the worst", since there's no safety concern
        # in letting a claim proceed without a consistency opinion for one
        # run (unlike injection detection).
        output = ConsistencyCheckOutput(
            has_conflicts=False, conflicts=[],
            summary=f"Consistency check failed to run: {type(e).__name__}: {e}",
        )

    duration_ms = int((time.time() - start_time) * 1000)

    log_agent_action(
        claim_id=claim_id,
        agent_name=AGENT_NAME,
        action="consistency_check",
        output_summary={
            "has_conflicts": output.has_conflicts,
            "conflict_count": len(output.conflicts),
            "severities": [c.severity.value for c in output.conflicts],
        },
        duration_ms=duration_ms,
    )

    logger.info(
        f"[{claim_id}] Consistency check complete: has_conflicts={output.has_conflicts}, "
        f"{len(output.conflicts)} finding(s)"
    )

    return {
        "consistency_output": output,
        "pipeline_trace": [{
            "agent": AGENT_NAME,
            "has_conflicts": output.has_conflicts,
            "duration_ms": duration_ms,
            "confidence": None,
            "decision": "conflicts_found" if output.has_conflicts else "consistent",
            "reasoning": output.summary,
            "flags": [f"{c.conflict_type.value} ({c.severity.value}): {c.description}" for c in output.conflicts],
            "findings": {
                "conflicts": [
                    {
                        "type": c.conflict_type.value, "severity": c.severity.value,
                        "description": c.description, "retry_target": c.retry_target,
                    }
                    for c in output.conflicts
                ],
            },
        }],
        "agent_call_count": 1,  # operator.add reducer sums this into the running total - see state.py
    }
