"""Loads and parses the real insurance_claims.csv into InsuranceClaimRow
objects, scoped to this project's auto_collision focus.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from src.data.claim_row import InsuranceClaimRow

_CSV_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "insurance_claims.csv"

# Our ClaimType.AUTO_COLLISION maps to both of these real incident_type
# values - the dataset doesn't distinguish "collision" from "multi-vehicle
# collision" the way a coverage policy would; both are collision damage.
_COLLISION_INCIDENT_TYPES = {"Multi-vehicle Collision", "Single Vehicle Collision"}


def _parse_bool(value: str) -> bool | None:
    if value == "YES":
        return True
    if value == "NO":
        return False
    return None  # "?" or blank - genuinely unknown, not False


def _row_from_csv_dict(d: dict) -> InsuranceClaimRow:
    return InsuranceClaimRow(
        policy_number=d["policy_number"],
        policy_deductable=Decimal(d["policy_deductable"]),
        policy_annual_premium=Decimal(d["policy_annual_premium"]),
        incident_date=d["incident_date"],
        incident_type=d["incident_type"],
        incident_severity=d["incident_severity"],
        incident_state=d["incident_state"],
        incident_city=d["incident_city"],
        incident_location=d["incident_location"],
        number_of_vehicles_involved=int(d["number_of_vehicles_involved"]),
        police_report_available=_parse_bool(d["police_report_available"]),
        total_claim_amount=Decimal(d["total_claim_amount"]),
        vehicle_claim=Decimal(d["vehicle_claim"]),
        auto_make=d["auto_make"],
        auto_model=d["auto_model"],
        auto_year=int(d["auto_year"]),
        fraud_reported=d["fraud_reported"] == "Y",
    )


@lru_cache(maxsize=1)
def load_all_claims() -> tuple[InsuranceClaimRow, ...]:
    """All 1000 real rows, parsed. Cached - the file never changes at runtime."""
    with open(_CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return tuple(_row_from_csv_dict(row) for row in reader)


@lru_cache(maxsize=1)
def load_auto_collision_claims() -> tuple[InsuranceClaimRow, ...]:
    """Real rows scoped to this project's auto_collision focus (822 of the
    1000 real rows - excludes Vehicle Theft and Parked Car, which don't map
    to our auto_collision document set)."""
    return tuple(r for r in load_all_claims() if r.incident_type in _COLLISION_INCIDENT_TYPES)
