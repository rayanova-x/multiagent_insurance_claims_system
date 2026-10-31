"""
Damage Assessment Agent (LangGraph Node)

Analyzes the claimed damage and produces an independent assessment of:
- Total damage amount (may differ from claimant's estimate)
- Line-item breakdown
- Repair vs replace vs total loss recommendation
- Whether physical inspection is needed

METHODOLOGY AND ENFORCEMENT (per CLAUDE.md "hard rule checks enforced in
code"): the company's total-loss threshold (repair/ACV >= 0.75) is a real
regulatory-style compliance line, not adjuster discretion - most US states
set a statutory or company-discretionary total-loss threshold in the 65-80%
range, and insurers are bound to apply theirs consistently once set
(verified against real-world practice, not just this codebase's own
comment). Four rules are therefore CODE-ENFORCED after the LLM call, never
left to the prompt alone - see _enforce_hard_rules():
  1. should_total_loss()'s own ratio verdict forces repair_vs_replace, even
     if the LLM recommends otherwise.
  2. repair_vs_replace == "total_loss" forces assessed_damage_usd to the
     computed ACV, never a repair-cost figure, even if the LLM returns one.
  3. assessed_damage_usd is capped to stay close to the repair-estimate
     document's real stated_total when one exists, so the LLM's job stays
     "flag a specific implausible line item" rather than "invent a
     replacement figure."
  4. No repair-estimate document at all -> requires_physical_inspection is
     forced True, structurally, not just asked for in the prompt.

GROUNDING FOR THE REPAIR-COST NUMBER ITSELF: there is no real external
database of "typical repair costs" available to this system (checked - the
real industry tools, Mitchell/CCC ONE/Audatex, are paid, contract-based,
not accessible here). An earlier version of this module used a hardcoded,
UNSOURCED table of 10 made-up cost ranges matched by fragile description
keyword-search (get_repair_estimate_range, now retired) - this violated
CLAUDE.md's "schema validity is not correctness" in the opposite direction:
inventing a fake-but-plausible-looking number is not better than letting
the model invent one directly. The fix: the repair-estimate DOCUMENT's own
real, claim-specific line items (when one was submitted) are the grounding
- not a generic lookup table. The LLM's job narrows to flagging
implausible-looking items using its own general knowledge, never replacing
the shop's real pricing wholesale with an independently invented figure.
When no repair-estimate document exists at all, the assessment is honestly
ungrounded and requires_physical_inspection is forced True rather than
silently guessing.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src.llm import get_structured_llm
from src.models.schemas import DamageAssessmentOutput
from src.models.state import ClaimsState
from src.security.audit_log import log_agent_action
from src.tools.damage_calculator import (
    apply_depreciation,
    calculate_vehicle_acv,
    should_total_loss,
)
from src.utils import calculate_asset_age, currency_symbol, detect_asset_type

logger = logging.getLogger(__name__)
AGENT_NAME = "damage_assessor"

# Rule 3: when a repair-estimate document exists, assessed_damage_usd
# should track its stated_total closely - the LLM's job is to flag
# specific implausible line items and adjust by THAT item's amount, not to
# discard the shop's real pricing and substitute an independently invented
# figure (see SYSTEM_PROMPT step 4). A deviation beyond this fraction of
# stated_total is treated as the LLM having done the latter, and is capped
# back toward the document's real total.
_MAX_DEVIATION_FROM_STATED_TOTAL = 0.40  # 40% - allows a genuinely flagged line item, not a full rewrite

SYSTEM_PROMPT = """You are a certified insurance damage assessor for this
company. Your assessments are independent, objective, and follow this
company's own documented methodology below - not generic industry
knowledge, and not the claimant's own characterization of severity in
their description.

COMPANY METHODOLOGY (apply exactly, in this order):

1. ACTUAL CASH VALUE (ACV): the vehicle's current worth, already computed
   for you by code (see PRE-COMPUTED TOOL DATA below) using this company's
   depreciation schedule: 20% year 1, 15% year 2, 12% year 3, 9%/year
   years 4-5, 7%/year after that, compounded by vehicle age. Use the
   provided ACV exactly - do not recompute or estimate it yourself.

2. TOTAL LOSS DETERMINATION: if the repair cost reaches or exceeds 75% of
   ACV (repair_cost / ACV >= 0.75), this is a TOTAL LOSS. This is a hard
   company threshold, not a judgment call - apply it exactly as computed
   in the PRE-COMPUTED TOOL DATA, regardless of how the claimant's own
   description characterizes the severity (a description can say "minor
   damage" while the real repair-to-ACV ratio still crosses 75%, and a
   description can say "total loss" while the ratio stays well under 75%
   - trust the computed ratio, never descriptive language in the text).

3. IF TOTAL LOSS: assessed_damage_usd MUST equal the ACV exactly - never a
   repair-cost figure, never a blend of the two. repair_vs_replace must be
   "total_loss".

4. IF NOT TOTAL LOSS: assessed_damage_usd is grounded in the repair shop's
   own itemized estimate when one is provided (see REPAIR ESTIMATE LINE
   ITEMS below) - that is real, specific pricing for THIS vehicle's THIS
   damage, not a generic lookup. Your job is NOT to invent a competing
   number from general knowledge - it is to USE the shop's stated total as
   your assessed_damage_usd UNLESS a specific line item looks genuinely
   implausible for the described damage (e.g. a windshield replacement
   priced at $40,000, or a line item unrelated to the described damage).
   If you flag a line item as implausible, explain exactly which one and
   why in assessment_notes, and adjust assessed_damage_usd only by the
   amount that specific item is off by - never discard the whole estimate
   and substitute your own independent figure. repair_vs_replace is
   "repair" (or "replace" for a component cheaper to replace outright than
   repair, short of the full vehicle being a total loss).

   IF NO REPAIR ESTIMATE DOCUMENT WAS PROVIDED: there is no real pricing
   to ground an assessment in. Do not invent a plausible-sounding number
   from general knowledge - set assessed_damage_usd to the claimant's own
   estimated_amount as a placeholder pending inspection, and you MUST set
   requires_physical_inspection=True (see step 5) - an assessment with no
   real cost source behind it is not independently verified, regardless of
   how confident the description sounds.

5. PHYSICAL INSPECTION: set requires_physical_inspection=True whenever:
   (a) no repair-estimate document was provided (mandatory - see step 4),
   (b) the description is too vague to make sense of the line items, or
   (c) the computed ratio is close to the 75% threshold (within ~10
   percentage points either side) where a misjudgment has real
   consequences.

WORKED EXAMPLES (real claims from this company's own data):

Example A - REPAIR (ratio stays under the threshold despite the description):
  Vehicle: 2020 Audi A3, age 6 years
  Description: "Multi-vehicle Collision involving 3 vehicles... Severity
    assessed as total loss."
  PRE-COMPUTED: ACV = $20,000.00. Claimant's repair estimate = $13,500.00.
    Ratio = 13,500 / 20,000 = 0.675 (67.5%) - BELOW the 75% threshold.
  Correct reasoning: Despite the description's own word "total loss", the
    computed ratio (67.5%) is below the company's 75% threshold. This is a
    REPAIR case, not total loss - the description's characterization is
    not authoritative; the computed ratio is.
  Correct output: repair_vs_replace="repair", assessed_damage_usd≈$13,500
    (an independent repair estimate near the claimant's figure, adjusted
    only if the itemized description warrants it), requires_physical_
    inspection=True (ratio is within 10 points of the threshold - 67.5%
    is close enough that a misjudgment matters).

Example B - TOTAL LOSS (ratio crosses the threshold despite the description):
  Vehicle: 2018 Dodge RAM, age 8 years
  Description: "Multi-vehicle Collision involving 3 vehicles... Severity
    assessed as minor damage."
  PRE-COMPUTED: ACV = $13,000.00. Claimant's repair estimate = $23,100.00.
    Ratio = 23,100 / 13,000 = 1.777 (177.7%) - WELL ABOVE the 75% threshold.
  Correct reasoning: The description says "minor damage", but the computed
    ratio (177.7%) is far above the 75% threshold - repairing would cost
    nearly 1.8x what the vehicle is worth. This is a TOTAL LOSS regardless
    of the description's wording.
  Correct output: repair_vs_replace="total_loss", assessed_damage_usd=
    $13,000.00 (the ACV exactly, NOT the $23,100 repair estimate),
    requires_physical_inspection=False (the ratio is unambiguously over
    the threshold, not a close call).

Be neither too generous nor too conservative - aim for accurate fair value
under this methodology, not the claimant's estimate and not the cheapest
possible outcome."""


def _enforce_hard_rules(
    output: DamageAssessmentOutput,
    depreciation_info: dict | None,
    has_repair_document: bool,
    claim_id: str,
) -> None:
    """Code-enforced hard rules, applied after the LLM responds, overriding
    whatever it returned if it disagrees. A prompt is not a control - see
    this module's docstring. Mutates `output` in place.
    """
    if not depreciation_info:
        return

    # Rule 1: should_total_loss()'s own computed ratio decides
    # repair_vs_replace, never the LLM's own judgment of the description.
    if depreciation_info["is_total_loss"] and output.repair_vs_replace != "total_loss":
        logger.warning(
            f"[{claim_id}] Code-enforced total_loss: computed ratio "
            f"{depreciation_info['total_loss_ratio']*100:.1f}% >= 75%, LLM said '{output.repair_vs_replace}'"
        )
        output.repair_vs_replace = "total_loss"
        output.assessment_notes += (
            f" [Code-enforced: repair/ACV ratio ({depreciation_info['total_loss_ratio']*100:.1f}%) "
            "meets the 75% total-loss threshold.]"
        )
    elif not depreciation_info["is_total_loss"] and output.repair_vs_replace == "total_loss":
        logger.warning(
            f"[{claim_id}] Code-enforced non-total_loss: computed ratio "
            f"{depreciation_info['total_loss_ratio']*100:.1f}% < 75%, LLM said 'total_loss'"
        )
        output.repair_vs_replace = "repair"
        output.assessment_notes += (
            f" [Code-enforced: repair/ACV ratio ({depreciation_info['total_loss_ratio']*100:.1f}%) "
            "is below the 75% total-loss threshold.]"
        )

    # Rule 2: total_loss always pays the ACV, never a repair-cost figure -
    # re-checked AFTER rule 1, so this also covers the case where rule 1
    # just flipped the recommendation to total_loss.
    if output.repair_vs_replace == "total_loss":
        acv = depreciation_info["estimated_acv"]
        if output.assessed_damage_usd != acv:
            output.assessment_notes += f" [Code-enforced: assessed amount set to ACV ({acv:,.2f}).]"
            output.assessed_damage_usd = acv
        return  # Rule 3 (repair-range outlier check) does not apply to a total-loss payout.

    # Rule 3: when a real repair-estimate document exists, assessed_damage_usd
    # should track its stated_total closely - the LLM's job (per SYSTEM_PROMPT
    # step 4) is to flag SPECIFIC implausible line items and adjust by that
    # item's amount, never to discard real shop pricing and substitute an
    # independently invented figure (which would be exactly the "typical
    # cost" guessing this design deliberately moved away from - see CLAUDE.md
    # "schema validity is not correctness" / the repair-cost-range retirement
    # this rule replaces). A deviation beyond _MAX_DEVIATION_FROM_STATED_TOTAL
    # is treated as the LLM having done that, and is capped back.
    stated_total = depreciation_info.get("repair_estimate_stated_total")
    if stated_total:
        max_dev = stated_total * _MAX_DEVIATION_FROM_STATED_TOTAL
        floor, ceiling = stated_total - max_dev, stated_total + max_dev
        if not (floor <= output.assessed_damage_usd <= ceiling):
            logger.warning(
                f"[{claim_id}] Code-enforced deviation cap: assessed {output.assessed_damage_usd:,.2f} "
                f"deviates >{_MAX_DEVIATION_FROM_STATED_TOTAL*100:.0f}% from the repair estimate's "
                f"stated_total ({stated_total:,.2f})"
            )
            clipped = min(max(output.assessed_damage_usd, floor), ceiling)
            output.assessment_notes += (
                f" [Code-enforced: assessed amount adjusted to stay within "
                f"{_MAX_DEVIATION_FROM_STATED_TOTAL*100:.0f}% of the repair estimate's stated total "
                f"({stated_total:,.2f}) - the model's figure deviated further than a specific flagged "
                f"line item would justify.]"
            )
            output.assessed_damage_usd = clipped

    # Rule 4: no repair-estimate document at all -> no real pricing exists to
    # ground this assessment in, so physical inspection is mandatory
    # regardless of what the LLM returned. Structural enforcement, not just
    # a prompt instruction (step 5/CLAUDE.md "structural fixes over prompt
    # wording") - an LLM asked nicely to set this flag can still forget.
    if not has_repair_document and not output.requires_physical_inspection:
        logger.warning(f"[{claim_id}] Code-enforced inspection requirement: no repair-estimate document available")
        output.requires_physical_inspection = True
        output.assessment_notes += (
            " [Code-enforced: no repair-estimate document was available, so this assessment has no "
            "real pricing to ground it - physical inspection is required.]"
        )


def _repair_cost_estimate(state: ClaimsState, claim: dict) -> tuple[float, str]:
    """The vehicle-specific repair cost to ground the total-loss ratio
    against. Prefers the repair-estimate document's stated_total (real
    vehicle damage cost) over claim.estimated_amount, which is the
    claimant's SELF-REPORTED TOTAL claim figure - in the real dataset this
    can include injury/property/liability costs well beyond vehicle damage
    (verified during calibration, see project memory
    project-smart-claims-dataset-calibration), so using it for a
    vehicle-specific total-loss ratio over-triggers total_loss. Falls back
    to estimated_amount when no document data is available (claim_documents
    is None for most claims today - see ClaimsState's field comment).
    Returns (amount, source_label) so callers can note which was used.
    """
    docs = state.get("claim_documents")
    if docs:
        repair_estimate = docs.get("repair_estimate")
        if repair_estimate and repair_estimate.get("stated_total") is not None:
            return float(repair_estimate["stated_total"]), "repair_estimate document"
    return float(claim.get("estimated_amount", 0)), "claimant's estimated_amount (no repair-estimate document available)"


def _repair_estimate_line_items(state: ClaimsState) -> Optional[list[dict]]:
    """The repair-estimate document's real itemized line items, when one
    exists. Returns None (not an empty list) when no repair-estimate
    document was provided at all - the caller needs to distinguish "no
    document, no real pricing to ground on" from "document exists but has
    zero line items" (an empty list would be a confirmed fact, not an
    absence - see CLAUDE.md 'schemas must have a legitimate I don't know
    path'). An empty-but-present document still counts as "provided" for
    the mandatory-inspection rule, since stated_total alone is still real
    pricing even with no item-level breakdown.
    """
    docs = state.get("claim_documents")
    if not docs:
        return None
    repair_estimate = docs.get("repair_estimate")
    if not repair_estimate:
        return None
    items = repair_estimate.get("line_items") or []
    # amount arrives as a str here when documents were round-tripped through
    # JSON (Pydantic's Decimal fields serialize to JSON strings) - normalize
    # to float so every downstream consumer (prompt formatting, any future
    # arithmetic) gets a numeric type regardless of how the document got here.
    return [{**item, "amount": float(item["amount"])} for item in items]


def run_damage_assessor(state: ClaimsState) -> dict:
    """LangGraph node for damage assessment."""
    claim = state["claim"]
    claim_id = claim["claim_id"]
    masked_claim = state.get("masked_claim", {})
    start_time = time.time()

    logger.info(f"[{claim_id}] Damage assessment started")

    # ── Pre-compute from tools (reduces LLM hallucination) ────────────────────
    asset_type = detect_asset_type(claim.get("incident_type", ""))
    asset_age = calculate_asset_age(claim.get("vehicle_year"), claim.get("incident_date"))
    acv = None
    depreciation_info = None
    line_items_from_doc = _repair_estimate_line_items(state)

    if asset_type == "auto":
        estimated, estimated_source = _repair_cost_estimate(state, claim)

        if claim.get("vehicle_year"):
            acv = calculate_vehicle_acv(
                year=int(claim["vehicle_year"]),
                make=claim.get("vehicle_make", ""),
                model=claim.get("vehicle_model", ""),
                as_of=claim.get("incident_date"),
            )
            is_total_loss, tl_ratio = should_total_loss(estimated, acv)
            _, depreciation_amount = apply_depreciation(estimated, "auto", asset_age)
            depreciation_info = {
                "asset_age_years": asset_age,
                "estimated_acv": acv,
                "is_total_loss": is_total_loss,
                "total_loss_ratio": tl_ratio,
                "depreciation_applied_usd": depreciation_amount,
                "repair_cost_used": estimated,
                "repair_cost_source": estimated_source,
            }
            if line_items_from_doc is not None:
                depreciation_info["repair_estimate_stated_total"] = estimated
        else:
            # No vehicle year - still provide what grounding exists
            depreciation_info = {
                "asset_age_years": 0,
                "estimated_acv": estimated,
                "is_total_loss": False,
                "total_loss_ratio": 0.0,
                "depreciation_applied_usd": 0.0,
            }

    # ── LLM Assessment ────────────────────────────────────────────────────────
    llm = get_structured_llm(DamageAssessmentOutput)

    _s = currency_symbol()

    tool_context = ""
    if depreciation_info:
        repair_cost_line = ""
        if "repair_cost_used" in depreciation_info:
            repair_cost_line = (
                f"        - Repair Cost Used For Ratio: {_s}{depreciation_info['repair_cost_used']:,.2f} "
                f"(source: {depreciation_info['repair_cost_source']})\n"
            )
        tool_context = f"""
        PRE-COMPUTED TOOL DATA (use as grounding for your assessment):
        - Vehicle ACV (Actual Cash Value): {_s}{depreciation_info['estimated_acv']:,.2f}
        - Vehicle Age: {depreciation_info['asset_age_years']} years
{repair_cost_line}        - Estimated Depreciation on Claimed Amount: {_s}{depreciation_info['depreciation_applied_usd']:,.2f}
        - Total Loss Check: {'YES - repair cost ({:.1f}%) exceeds 75% of ACV'.format(depreciation_info['total_loss_ratio']*100) if depreciation_info['is_total_loss'] else 'No (repair cost is {:.1f}% of ACV)'.format(depreciation_info['total_loss_ratio']*100)}
        """
        if line_items_from_doc:
            items_text = "\n".join(
                f"          - {item['description']}: {_s}{item['amount']:,.2f}" for item in line_items_from_doc
            )
            tool_context += (
                "\n        REPAIR ESTIMATE LINE ITEMS (from the shop's own document - real pricing for "
                f"THIS vehicle, use as your primary grounding per step 4):\n{items_text}\n"
            )
        elif line_items_from_doc is None:
            tool_context += (
                "\n        REPAIR ESTIMATE DOCUMENT: NOT PROVIDED. There is no real pricing to ground "
                "this assessment in - per step 4, you MUST set requires_physical_inspection=True.\n"
            )

    # Set only by graph.py's prepare_consistency_retry_node, when the
    # consistency coordinator (src/agents/consistency_coordinator.py) found
    # a SEVERITY_MISMATCH conflict between this assessment and fraud_crew's
    # narrative read - a second, informed pass, not a blind retry. Absent
    # on a normal (first) run.
    retry_context = state.get("consistency_retry_context") or ""

    prompt = f"""
    Assess the damage for this insurance claim, following the company
    methodology and worked examples in your instructions exactly:

    CLAIM TYPE: {claim.get('incident_type', 'unknown').upper()}
    INCIDENT DESCRIPTION: {masked_claim.get('incident_description', claim.get('incident_description', 'Not provided'))}
    CLAIMANT'S ESTIMATED AMOUNT: {_s}{float(claim.get('estimated_amount', 0)):,.2f}
    VEHICLE: {claim.get('vehicle_year', 'N/A')} {claim.get('vehicle_make', '')} {claim.get('vehicle_model', '')}
    DOCUMENTS PROVIDED: {', '.join(claim.get('documents', [])) or 'None'}
    {tool_context}
    {retry_context}

    All amounts must be in the SAME currency as the claimant's estimate ({_s}).

    Produce an independent damage assessment with:
    1. Your assessed total damage amount, per the methodology (step 3 if
       total loss, step 4 otherwise - see instructions)
    2. Itemized breakdown of damage components with amounts in {_s}
    3. repair_vs_replace, per the computed ratio (step 2), never the
       description's own characterization of severity
    4. Whether physical inspection is needed (step 5)
    5. Confidence in your assessment
    """

    raw_llm_output = None
    try:
        output = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        # Captured BEFORE _enforce_hard_rules mutates output in place - the
        # admin debug view can then show the LLM's real answer separately
        # from whichever hard rule overrode it.
        raw_llm_output = output.model_dump(mode="json")
        _enforce_hard_rules(output, depreciation_info, line_items_from_doc is not None, claim_id)
    except Exception as e:
        logger.error(f"[{claim_id}] Damage assessor LLM failed: {e}")
        # Fall back to the ACV when the tool data already says total loss -
        # the same hard rule applies even on the error path, not just the
        # happy path.
        fallback_amount = float(claim.get("estimated_amount", 0))
        fallback_notes = f"LLM error during assessment: {str(e)}. Using claimant estimate pending manual review."
        if depreciation_info and depreciation_info.get("is_total_loss"):
            fallback_amount = depreciation_info["estimated_acv"]
            fallback_notes = (
                f"LLM error during assessment: {str(e)}. Pre-computed ratio already indicated total loss "
                f"({depreciation_info['total_loss_ratio']*100:.1f}% of ACV) - using ACV pending manual review."
            )
        output = DamageAssessmentOutput(
            assessed_damage_usd=fallback_amount,
            line_items=[{"item": "Unable to itemize - LLM error", "amount": fallback_amount}],
            repair_vs_replace="total_loss" if (depreciation_info and depreciation_info.get("is_total_loss")) else "repair",
            assessment_confidence=0.30,
            assessment_notes=fallback_notes,
            requires_physical_inspection=True,
            comparable_claims_avg=None,
        )

    duration_ms = int((time.time() - start_time) * 1000)

    log_agent_action(
        claim_id=claim_id,
        agent_name=AGENT_NAME,
        action="damage_assessment",
        input_summary={"claim_type": claim.get("incident_type"), "estimated": claim.get("estimated_amount")},
        output_summary={
            "assessed_amount": output.assessed_damage_usd,
            "recommendation": output.repair_vs_replace,
            "confidence": output.assessment_confidence,
        },
        duration_ms=duration_ms,
    )

    cs = currency_symbol()
    logger.info(
        f"[{claim_id}] Damage assessed: {cs}{output.assessed_damage_usd:,.2f} "
        f"(claimant: {cs}{float(claim.get('estimated_amount', 0)):,.2f}), "
        f"recommendation={output.repair_vs_replace}"
    )

    return {
        "damage_output": output,
        "pipeline_trace": [{
            "agent": AGENT_NAME,
            "assessed_usd": output.assessed_damage_usd,
            "vs_claimed_usd": float(claim.get("estimated_amount", 0)),
            "confidence": output.assessment_confidence,
            "duration_ms": duration_ms,
            "decision": output.repair_vs_replace,
            "reasoning": output.assessment_notes,
            "flags": ["requires_physical_inspection"] if output.requires_physical_inspection else [],
            "findings": {
                "assessed_damage": output.assessed_damage_usd,
                "claimant_estimate": float(claim.get("estimated_amount", 0)),
                "recommendation": output.repair_vs_replace,
                "line_items_count": len(output.line_items),
                "requires_inspection": output.requires_physical_inspection,
            },
            "calls": [{"label": "damage_assessment", "prompt": prompt, "raw_output": raw_llm_output}],
        }],
        "agent_call_count": 1,  # operator.add reducer sums this into the running total - see state.py
        "consistency_retry_context": None,  # consumed - don't leak into a later, unrelated run
    }
