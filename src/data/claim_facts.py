"""Builds one ClaimFacts object per claim, before any document field is
generated - the single source of truth every document-field generator reads
from, so the claim's documents stay internally consistent (same incident
description, same claimant name, same vehicle) rather than drifting if
generated independently. Same reasoning as casefile's
casefile/data/claim_facts.py, adapted to this project's narrower scope
(auto_collision only, 3 active document types).

Names are synthetic and deterministic, seeded by policy_number - the real
dataset has no name field. Reproducible across runs, obviously placeholder
data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.data.claim_row import InsuranceClaimRow

_FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Moore", "Lee", "Thompson",
]


def _deterministic_name(seed: str) -> str:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    first = _FIRST_NAMES[h % len(_FIRST_NAMES)]
    last = _LAST_NAMES[(h // len(_FIRST_NAMES)) % len(_LAST_NAMES)]
    return f"{first} {last}"


def _deterministic_digits(seed: str, n: int) -> str:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return str(h % (10 ** n)).zfill(n)


@dataclass
class ClaimFacts:
    """One settled set of facts for one synthetic claim, derived from a real
    InsuranceClaimRow. Every document-field generator reads from this, never
    from the row directly, so all documents for one claim agree."""

    row: InsuranceClaimRow
    claim_id: str
    claimant_name: str
    claimant_email: str
    claimant_phone: str
    claimant_dob: str
    other_driver_name: Optional[str]
    other_driver_license: Optional[str]
    other_driver_insurer: Optional[str]
    other_driver_vehicle_make: Optional[str]
    other_driver_vehicle_model: Optional[str]
    other_driver_vehicle_year: Optional[int]
    police_report_number: Optional[str]
    repair_shop_name: str
    narrative: str


_REPAIR_SHOPS = ["Main St Auto Body", "Precision Collision Center", "Lakeside Auto Repair", "Summit Body Shop"]
_OTHER_INSURERS = ["SafeCo", "Pinnacle Mutual", "Harbor Insurance", "Crestline Auto Insurance"]
_OTHER_MAKES_MODELS = [
    ("Honda", "Civic"), ("Toyota", "Corolla"), ("Ford", "F-150"), ("Chevrolet", "Malibu"),
]


def build_claim_facts(row: InsuranceClaimRow, claim_id: str, incident_date: str | None = None) -> ClaimFacts:
    """incident_date overrides row.incident_date for the narrative and police
    report number - generate.py may shift the real row's date forward (see
    _shift_incident_date) so the claim falls inside the test policy's
    coverage window; passing it here keeps the free-text narrative
    consistent with the structured incident_date field, rather than the two
    silently disagreeing. Defaults to the row's own date when not given
    (e.g. the lapsed_policy category, which deliberately does not shift)."""
    incident_date = incident_date or row.incident_date
    incident_year = int(incident_date[:4])
    seed = f"claim:{claim_id}:{row.policy_number}"
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)

    other_driver_involved = row.number_of_vehicles_involved > 1
    other_make, other_model = _OTHER_MAKES_MODELS[h % len(_OTHER_MAKES_MODELS)]

    vehicles = "a single vehicle" if row.number_of_vehicles_involved == 1 else f"{row.number_of_vehicles_involved} vehicles"
    narrative = (
        f"{row.incident_type} involving {vehicles} in {row.incident_city}, {row.incident_state}, "
        f"at {row.incident_location} on {incident_date}. Severity assessed as "
        f"{row.incident_severity.lower()}."
    )

    return ClaimFacts(
        row=row,
        claim_id=claim_id,
        claimant_name=_deterministic_name(f"claimant:{seed}"),
        claimant_email=f"claimant.{_deterministic_digits(seed, 6)}@example.com",
        claimant_phone=f"555-{_deterministic_digits('phone:' + seed, 3)}-{_deterministic_digits('phone2:' + seed, 4)}",
        claimant_dob="1985-03-15",  # placeholder, not derived from the real row (no DOB column exists)
        other_driver_name=_deterministic_name(f"other_driver:{seed}") if other_driver_involved else None,
        other_driver_license=_deterministic_digits(f"license:{seed}", 8) if other_driver_involved else None,
        other_driver_insurer=_OTHER_INSURERS[h % len(_OTHER_INSURERS)] if other_driver_involved else None,
        other_driver_vehicle_make=other_make if other_driver_involved else None,
        other_driver_vehicle_model=other_model if other_driver_involved else None,
        # Relative to the incident year, not a hardcoded 2015-2023 window -
        # otherwise the other driver's car goes stale the same way the
        # claimant's own vehicle_year did before being shifted (see
        # generate.py's _shift_year comment). A used car 0-8 years old at
        # the time of the incident is a plausible spread.
        other_driver_vehicle_year=(incident_year - (h % 9)) if other_driver_involved else None,
        police_report_number=(
            f"{row.incident_state}PD-{incident_date.replace('-', '')}-{_deterministic_digits(seed, 4)}"
            if row.police_report_available else None
        ),
        repair_shop_name=_REPAIR_SHOPS[h % len(_REPAIR_SHOPS)],
        narrative=narrative,
    )
