"""Generates the stratified synthetic claim files from the real dataset.

For each selected ClaimPlan: builds one ClaimFacts (the single source of
truth for that claim's identity/narrative), then writes two JSON files -
one ClaimInput-shaped claim submission (what a claimant would actually
submit through the API), and one structured-document-fields file (the
TARGET SHAPE a future PDF-extraction capability would produce - see
src/models/documents.py's module docstring). Both derived from the same
ClaimFacts, so they stay consistent with each other.

Run with: python -m src.data.generate
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from src.data.claim_facts import ClaimFacts, build_claim_facts
from src.data.selection import (
    ClaimPlan,
    build_100_test_set,
    build_large_nonfraud_selection,
    build_stratified_selection,
)
from src.models.documents import (
    AutoCollisionDocuments,
    OtherDriverInfoFields,
    PoliceReportFields,
    RepairEstimateFields,
    RepairLineItem,
)

_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "stratified_claims"
# Separate directory for build_large_nonfraud_selection()'s output - keeps
# the original ~47-claim mix (data/stratified_claims/) untouched, since
# other code/tests may already reference it by its existing claim_ids.
_LARGE_NONFRAUD_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "large_nonfraud_claims"
# Separate directory for build_100_test_set()'s output - the 17-category,
# rule-driven test set (see that function's own docstring).
_HUNDRED_TEST_SET_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "test_set_100"

_POLICY_NUMBER_DEFAULT = "POL-AUTO-TEST-US"
_POLICY_NUMBER_HIGH_DEDUCTIBLE = "POL-AUTO-HIGHDED-US"

_EXCLUSION_NARRATIVES = {
    "racing": "Claimant states the vehicles were racing on a closed stretch of road when the collision occurred.",
    "commercial_use": "Claimant was operating the vehicle for a delivery service at the time of the incident.",
    "pre_existing_damage": "Claimant's repair shop notes the damaged panel already had unrepaired dents and "
                            "scraped paint from a prior incident, visible underneath the new damage.",
    # Added 2026-10-20 for build_100_test_set() - near-miss narratives,
    # matching src/agents/policy_checker.py's own documented "does NOT
    # apply to" negative criteria word for word.
    "racing_near_miss": "Claimant admits to driving aggressively and exceeding the speed limit, but there was no "
                         "other vehicle involved in a timed contest and no closed course.",
    "commercial_use_near_miss": "Claimant was giving a coworker a ride home after their shift, with no payment "
                                 "involved, when the collision occurred.",
    "pre_existing_damage_near_miss": "The vehicle is a 2009 model with significant cosmetic wear from age, but "
                                      "the repair shop confirms the newly claimed damage is isolated to this "
                                      "incident, not an old area.",
}

_SPARSE_NARRATIVE = "There was a collision. The vehicle was damaged. Not sure of the exact details."
_DISPUTE_REASON = ("Claimant believes this should be classified as auto_theft instead, since the vehicle "
                    "was later found abandoned.")
_INVALID_POLICY_NUMBER = "POL-DOES-NOT-EXIST-999"

# The real dataset's incident_date values are all 2015 (verified - every one
# of the 822 auto_collision rows). The seeded test policies cover 2025-01-01
# to 2027-01-01. Shifting by +11 years maps 2015 -> 2026, comfortably inside
# that window with room on both sides - preserves the real data's day/month
# and relative spacing, just makes claims look like current, active
# scenarios rather than a decade-old policy window. The lapsed_policy
# category (ClaimPlan.force_lapsed) deliberately skips this shift.
#
# vehicle_year is shifted by the SAME amount, for the same reason -
# otherwise a car that was plausibly ~8 years old relative to its real 2015
# incident becomes a nonsensical ~19 years old relative to the shifted 2026
# incident, while still carrying the real dataset's 2015-era claim amounts
# (total_claim_amount/vehicle_claim were generated against the vehicle's
# REAL age, not an artificially aged one). Running calculate_vehicle_acv
# (src/tools/damage_calculator.py) against the un-shifted vehicle_year
# showed every single claim in the dataset would trigger total_loss - the
# real claim amounts are far larger than any ACV an 11-31-year-old vehicle
# could plausibly have. Shifting vehicle_year restores the original,
# internally-consistent relationship between vehicle age and claim size
# that the real data had in its own 2015 context. See project memory
# project-smart-claims-dataset-calibration.
_YEAR_SHIFT = 11


def _shift_year(year: int) -> int:
    return year + _YEAR_SHIFT


def _shift_incident_date(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    try:
        return d.replace(year=d.year + _YEAR_SHIFT).isoformat()
    except ValueError:
        # Feb 29 on a real leap year landing on a non-leap shifted year.
        return (d.replace(year=d.year + _YEAR_SHIFT, day=28)).isoformat()


def _decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


def _build_claim_input(plan: ClaimPlan, facts: ClaimFacts, incident_date: str, vehicle_year: int) -> dict:
    row = plan.row
    narrative = facts.narrative
    if plan.sparse_narrative:
        # Full replacement, not an append - a sparse/ambiguous narrative is
        # defined by what it DOESN'T say, so appending to the normal
        # template would still leave the real specific facts in place.
        narrative = _SPARSE_NARRATIVE
    elif plan.force_exclusion:
        narrative = f"{narrative} {_EXCLUSION_NARRATIVES[plan.force_exclusion]}"
    if plan.injection_text:
        narrative = f"{narrative}{plan.injection_text}"

    policy_number = (
        _POLICY_NUMBER_HIGH_DEDUCTIBLE
        if plan.selection_tag == "deductible_exceeds_damage"
        else _INVALID_POLICY_NUMBER if plan.invalid_policy_number
        else _POLICY_NUMBER_DEFAULT
    )

    estimated_amount = (
        plan.force_amount_override if plan.force_amount_override is not None
        else float(row.total_claim_amount)
    )

    # Real bug found and fixed 2026-10-20 (deep-verification pass over
    # data/test_set_100/, confirmed to ALSO affect the two earlier generated
    # sets - data/stratified_claims/ and data/large_nonfraud_claims/, not
    # new to this build): "documents" previously just echoed
    # plan.documents_present verbatim, which assumes other_driver_info is
    # always real content whenever it's requested. But
    # src/data/claim_facts.py only populates other_driver_name (and
    # everything derived from it) when the real row's own
    # number_of_vehicles_involved > 1 - a real single-vehicle row has NO
    # other driver, regardless of what documents_present asked for, and
    # _build_documents() below already correctly omits other_driver_info in
    # that case (see its own "if ... and facts.other_driver_name" guard).
    # So the claim's own "documents" list was silently claiming the
    # claimant attached a document that was never actually generated - a
    # real, if harmless-so-far (no current hard rule reads this field),
    # internal inconsistency. Fixed to reflect what _build_documents()
    # will ACTUALLY produce, not what was merely requested.
    documents_present = list(plan.documents_present)
    if "other_driver_info" in documents_present and not facts.other_driver_name:
        documents_present.remove("other_driver_info")

    return {
        "claim_id": facts.claim_id,
        "policy_number": policy_number,
        "claimant_name": facts.claimant_name,
        "claimant_email": facts.claimant_email,
        "claimant_phone": facts.claimant_phone,
        "claimant_dob": facts.claimant_dob,
        "incident_date": incident_date,
        "incident_type": "auto_collision",
        "incident_description": narrative,
        "incident_location": f"{row.incident_location}, {row.incident_city}, {row.incident_state}",
        "police_report_number": facts.police_report_number,
        "estimated_amount": estimated_amount,
        "vehicle_year": vehicle_year,
        "vehicle_make": row.auto_make,
        "vehicle_model": row.auto_model,
        "documents": [f"{doc}.pdf" for doc in documents_present],
        "is_appeal": False,
        "original_claim_id": None,
        "type_disputed_by_claimant": plan.disputed_incident_type,
        "type_dispute_reason": _DISPUTE_REASON if plan.disputed_incident_type else None,
        # Not part of ClaimInput - carried for dataset review/debugging only,
        # the API submission layer should strip underscore-prefixed keys.
        "_selection_tag": plan.selection_tag,
        "_fraud_reported_ground_truth": row.fraud_reported,
        "_note": plan.note,
    }


def _build_documents(plan: ClaimPlan, facts: ClaimFacts, incident_date: str) -> AutoCollisionDocuments:
    row = plan.row
    present = set(plan.documents_present)

    police_report = None
    if "police_report" in present:
        police_report = PoliceReportFields(
            report_number=facts.police_report_number,
            jurisdiction=f"{row.incident_city} Police Department",
            incident_date=incident_date,
            incident_location=f"{row.incident_location}, {row.incident_city}, {row.incident_state}",
            narrative=facts.narrative,
            fault_determination=(
                "Other driver cited as at fault" if row.number_of_vehicles_involved > 1
                else "Single-vehicle incident, no third party at fault"
            ),
            citations_issued=[] if row.incident_severity == "Minor Damage" else ["Failure to maintain control"],
            parties_involved=(
                [facts.claimant_name, facts.other_driver_name]
                if facts.other_driver_name else [facts.claimant_name]
            ),
        )

    other_driver_info = None
    if "other_driver_info" in present and facts.other_driver_name:
        other_driver_info = OtherDriverInfoFields(
            driver_name=facts.other_driver_name,
            driver_license_number=facts.other_driver_license,
            insurer_name=facts.other_driver_insurer,
            vehicle_make=facts.other_driver_vehicle_make,
            vehicle_model=facts.other_driver_vehicle_model,
            vehicle_year=facts.other_driver_vehicle_year,
        )

    repair_estimate = None
    if "repair_estimate" in present:
        # vehicle_claim is the real dataset's own damage figure; split into
        # two plausible line items so line_items_sum has something to sum -
        # the split itself is synthetic, the total is the real row's value.
        total = row.vehicle_claim
        parts_amount = (total * Decimal("0.6")).quantize(Decimal("0.01"))
        labor_amount = (total - parts_amount).quantize(Decimal("0.01"))
        line_items = [
            RepairLineItem(description="Parts and materials", amount=parts_amount),
            RepairLineItem(description="Labor", amount=labor_amount),
        ]
        repair_estimate = RepairEstimateFields(
            shop_name=facts.repair_shop_name,
            estimate_date=incident_date,
            line_items=line_items,
            line_items_sum=parts_amount + labor_amount,  # computed by code, not by an LLM
            stated_total=total,
        )

    return AutoCollisionDocuments(
        police_report=police_report,
        other_driver_info=other_driver_info,
        repair_estimate=repair_estimate,
    )


def generate_all() -> list[Path]:
    plans = build_stratified_selection()
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for i, plan in enumerate(plans, start=1):
        claim_id = f"CLM-STRAT-{i:03d}"
        incident_date = (
            plan.row.incident_date if plan.force_lapsed else _shift_incident_date(plan.row.incident_date)
        )
        vehicle_year = (
            plan.row.auto_year if plan.force_lapsed else _shift_year(plan.row.auto_year)
        )
        facts = build_claim_facts(plan.row, claim_id, incident_date=incident_date)

        claim_input = _build_claim_input(plan, facts, incident_date, vehicle_year)
        documents = _build_documents(plan, facts, incident_date)

        claim_path = _OUTPUT_DIR / f"{claim_id}_claim.json"
        docs_path = _OUTPUT_DIR / f"{claim_id}_documents.json"

        claim_path.write_text(json.dumps(claim_input, indent=2, default=_decimal_to_float), encoding="utf-8")
        docs_path.write_text(documents.model_dump_json(indent=2), encoding="utf-8")

        written.extend([claim_path, docs_path])

    return written


def generate_large_nonfraud(n: int = 100) -> list[Path]:
    """Same per-claim build logic as generate_all(), against
    build_large_nonfraud_selection()'s bigger, non-fraud-only sample -
    see that function's own docstring for why this exists. Writes to a
    SEPARATE directory (_LARGE_NONFRAUD_OUTPUT_DIR) so it doesn't collide
    with or overwrite the original stratified set's claim_ids."""
    plans = build_large_nonfraud_selection(n)
    _LARGE_NONFRAUD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for i, plan in enumerate(plans, start=1):
        claim_id = f"CLM-LARGE-{i:03d}"
        incident_date = (
            plan.row.incident_date if plan.force_lapsed else _shift_incident_date(plan.row.incident_date)
        )
        vehicle_year = (
            plan.row.auto_year if plan.force_lapsed else _shift_year(plan.row.auto_year)
        )
        facts = build_claim_facts(plan.row, claim_id, incident_date=incident_date)

        claim_input = _build_claim_input(plan, facts, incident_date, vehicle_year)
        documents = _build_documents(plan, facts, incident_date)

        claim_path = _LARGE_NONFRAUD_OUTPUT_DIR / f"{claim_id}_claim.json"
        docs_path = _LARGE_NONFRAUD_OUTPUT_DIR / f"{claim_id}_documents.json"

        claim_path.write_text(json.dumps(claim_input, indent=2, default=_decimal_to_float), encoding="utf-8")
        docs_path.write_text(documents.model_dump_json(indent=2), encoding="utf-8")

        written.extend([claim_path, docs_path])

    return written


def generate_100_test_set() -> list[Path]:
    """Same per-claim build logic as generate_all(), against
    build_100_test_set()'s 17-category, non-fraud, rule-driven sample - see
    that function's own docstring for the full rationale. Writes to its own
    SEPARATE directory so it doesn't collide with either of the other two
    generated sets."""
    plans = build_100_test_set()
    _HUNDRED_TEST_SET_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for i, plan in enumerate(plans, start=1):
        claim_id = f"CLM-TEST100-{i:03d}"
        incident_date = (
            plan.row.incident_date if plan.force_lapsed else _shift_incident_date(plan.row.incident_date)
        )
        vehicle_year = (
            plan.row.auto_year if plan.force_lapsed else _shift_year(plan.row.auto_year)
        )
        facts = build_claim_facts(plan.row, claim_id, incident_date=incident_date)

        claim_input = _build_claim_input(plan, facts, incident_date, vehicle_year)
        documents = _build_documents(plan, facts, incident_date)

        claim_path = _HUNDRED_TEST_SET_OUTPUT_DIR / f"{claim_id}_claim.json"
        docs_path = _HUNDRED_TEST_SET_OUTPUT_DIR / f"{claim_id}_documents.json"

        claim_path.write_text(json.dumps(claim_input, indent=2, default=_decimal_to_float), encoding="utf-8")
        docs_path.write_text(documents.model_dump_json(indent=2), encoding="utf-8")

        written.extend([claim_path, docs_path])

    return written


if __name__ == "__main__":
    files = generate_all()
    print(f"Generated {len(files) // 2} stratified claims ({len(files)} files) into {_OUTPUT_DIR}")
