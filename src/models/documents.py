"""
Structured fields extracted from a claim's supporting documents.

These are the TARGET SHAPE a future PDF/OCR extraction capability will
produce - not something extracted yet. Real PDF parsing is explicitly out of
scope for now (see CLAUDE.md "Document extraction" decision); this schema
lets the rest of the pipeline (and the stratified synthetic dataset) be built
against a stable contract today, so extraction can be slotted in later
without reshaping anything downstream.

Two rules, same as every other schema in this project:
1. Arithmetic is never done by an LLM - line_items_sum on RepairEstimateFields
   is computed by code from line_items, never asked of a model.
2. A single-value field is Optional/None when genuinely absent from the
   source document. A list field defaults to an empty list, never None, when
   a parse positively confirms zero items - distinguishing "checked, found
   nothing" from "never checked."

Photos are explicitly on hold - no schema here yet for photo-derived fields.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    """Document types with a structured-field schema below. Grows as more
    claim types get their own document schemas - today this only covers
    auto_collision's four required documents (photos excluded, on hold)."""
    POLICE_REPORT = "police_report"
    OTHER_DRIVER_INFO = "other_driver_info"
    REPAIR_ESTIMATE = "repair_estimate"


class PoliceReportFields(BaseModel):
    """Fields extracted from a police accident report."""

    report_number: Optional[str] = Field(None, description="Police report reference number, if stated.")
    jurisdiction: Optional[str] = Field(None, description="Issuing police department / jurisdiction, if stated.")
    incident_date: Optional[str] = Field(None, description="Date of the incident per the report, ISO format, if stated.")
    incident_location: Optional[str] = Field(None, description="Location of the incident per the report, if stated.")
    narrative: Optional[str] = Field(None, description="The officer's account of what happened, if given.")
    fault_determination: Optional[str] = Field(
        None, description="Who the report assigns fault to, in the report's own words, if stated. Never inferred."
    )
    citations_issued: list[str] = Field(
        default_factory=list,
        description="Citations/violations issued, if any. Empty list means confirmed zero, not unchecked.",
    )
    parties_involved: list[str] = Field(
        default_factory=list,
        description="Names/roles of people the report names as involved. Empty list means confirmed zero named, not unchecked.",
    )


class OtherDriverInfoFields(BaseModel):
    """Fields extracted from the other driver's information exchange (the
    other party's name, insurer, and vehicle - what a claimant typically
    hands over after a collision)."""

    driver_name: Optional[str] = Field(None, description="Other driver's name, if stated.")
    driver_license_number: Optional[str] = Field(None, description="Other driver's license number, if stated.")
    insurer_name: Optional[str] = Field(None, description="Other driver's insurance company, if stated.")
    policy_number: Optional[str] = Field(None, description="Other driver's policy number, if stated.")
    vehicle_make: Optional[str] = Field(None, description="Other driver's vehicle make, if stated.")
    vehicle_model: Optional[str] = Field(None, description="Other driver's vehicle model, if stated.")
    vehicle_year: Optional[int] = Field(None, description="Other driver's vehicle year, if stated.")
    license_plate: Optional[str] = Field(None, description="Other driver's license plate, if stated.")


class RepairLineItem(BaseModel):
    description: str
    amount: Decimal


class RepairEstimateFields(BaseModel):
    """Fields extracted from a repair shop's damage estimate.

    stated_total is read directly from the document; line_items_sum is
    computed by code from line_items, never by a model - kept as two
    separate fields (rather than one) so the two can be cross-checked
    against each other, the way settlement_calculator already cross-checks
    assessed damage against the claimant's own estimate.
    """

    shop_name: Optional[str] = Field(None, description="Name of the repair shop, if stated.")
    estimate_date: Optional[str] = Field(None, description="Date of the estimate, ISO format, if stated.")
    line_items: list[RepairLineItem] = Field(
        default_factory=list,
        description="Individual line items, if the document itemizes them. Empty list means confirmed zero itemized lines (e.g. a lump-sum estimate), not unchecked.",
    )
    line_items_sum: Optional[Decimal] = Field(
        None,
        description="Sum of line_items, COMPUTED BY CODE from the parsed line items - never computed by a model.",
    )
    stated_total: Optional[Decimal] = Field(
        None, description="The total as printed on the estimate, read directly - kept separate from line_items_sum so the two can be compared."
    )


# ── Per-claim-type document bundle ──────────────────────────────────────────

class AutoCollisionDocuments(BaseModel):
    """All structured document fields available for one auto_collision claim.
    A field is None when that document was not provided for this claim -
    mirrors documents_present/missing_documents already tracked elsewhere
    (src/agents/intake_agent.py's find_missing_documents) rather than
    duplicating that list here."""

    police_report: Optional[PoliceReportFields] = None
    other_driver_info: Optional[OtherDriverInfoFields] = None
    repair_estimate: Optional[RepairEstimateFields] = None
