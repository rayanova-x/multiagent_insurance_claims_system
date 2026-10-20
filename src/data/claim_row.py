"""Typed representation of one row of the real source dataset.

Source: "insurance_claims" (ABDELRAHIM AQQAD, 2023), Mendeley Data V2,
DOI: 10.17632/992mh7dk9y.2, CC BY 4.0. 1000 real auto insurance claim
records. Same file casefile (D:\\insurance_agent) uses and verified;
re-verified here by byte-identical SHA-256 before copying into this
project's data/raw/.

This project is scoped to auto_collision only right now (see CLAUDE.md
"Rebuild status and ground rules"), so only the columns relevant to that
scope and to the HITL-trigger calibration analysis are modeled - this is
deliberately a narrower subset of fields than casefile's InsuranceClaimRow,
not a full re-port of it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class InsuranceClaimRow(BaseModel):
    policy_number: str
    policy_deductable: Decimal
    policy_annual_premium: Decimal

    incident_date: str
    incident_type: str
    incident_severity: str
    incident_state: str
    incident_city: str
    incident_location: str
    number_of_vehicles_involved: int
    police_report_available: Optional[bool] = None

    total_claim_amount: Decimal
    vehicle_claim: Decimal

    auto_make: str
    auto_model: str
    auto_year: int

    # Ground truth for fraud. Used only by the stratified claim selection
    # (data/stratified_claims/) and never read by any pipeline agent/node.
    fraud_reported: bool
