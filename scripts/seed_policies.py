"""
Seed policies.db with test policies for both US and India country profiles.

Run once after cloning:
    python scripts/seed_policies.py

Creates/updates data/policies.db with 4 test policies (2 US, 2 India) that
have valid start/end dates spanning 2025-2027, so the intake agent accepts
them. Also updates the 3 original sample policies to have non-expired dates.

Safe to re-run - uses INSERT OR REPLACE so existing rows are updated.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from src.tools.policy_lookup import DB_PATH, _get_engine  # noqa: E402 - shares the Postgres-or-SQLite engine

engine = _get_engine()

POLICIES = [
    # ── US test policies ─────────────────────────────────────────────────────
    {
        "policy_number": "POL-AUTO-TEST-US",
        "holder_name": "Test Claimant",
        "type": "auto",
        "status": "active",
        "start_date": "2025-01-01",
        "end_date": "2027-01-01",
        "deductible": 500,
        "coverage": {
            "collision": 50000,
            "comprehensive": 50000,
            "liability": 100000,
            "medical_payments": 5000,
            "uninsured_motorist": 50000,
            "rental_reimbursement": 1500,
        },
        "exclusions": ["racing", "commercial_use"],
        "premium_monthly": 145,
        "claims_count": 0,
    },
    {
        # High-deductible variant, used by the deductible_exceeds_damage
        # edge case in the stratified dataset (src/data/selection.py).
        # Deliberately unrealistic: $15,000 is set to exceed even the
        # SMALLEST real vehicle_claim in the whole 822-row auto_collision
        # dataset ($12,720 - the real dataset has no small claims at all, so
        # there is no real row a normal deductible would naturally exceed).
        # The claim's damage amount stays 100% real data; only this test
        # policy's deductible is synthetic - see the selection.py comment on
        # DEDUCTIBLE_EXCEEDS_DAMAGE_THRESHOLD for the paired constant.
        "policy_number": "POL-AUTO-HIGHDED-US",
        "holder_name": "Test Claimant",
        "type": "auto",
        "status": "active",
        "start_date": "2025-01-01",
        "end_date": "2027-01-01",
        "deductible": 15000,
        "coverage": {
            "collision": 50000,
            "comprehensive": 50000,
            "liability": 100000,
            "medical_payments": 5000,
            "uninsured_motorist": 50000,
            "rental_reimbursement": 1500,
        },
        "exclusions": ["racing", "commercial_use"],
        "premium_monthly": 95,
        "claims_count": 0,
    },
    {
        "policy_number": "POL-HOME-TEST-US",
        "holder_name": "Test Claimant",
        "type": "homeowners",
        "status": "active",
        "start_date": "2025-01-01",
        "end_date": "2027-01-01",
        "deductible": 2500,
        "coverage": {
            "dwelling": 350000,
            "personal_property": 100000,
            "liability": 300000,
            "additional_living": 50000,
        },
        "exclusions": ["flood", "earthquake"],
        "premium_monthly": 185,
        "claims_count": 0,
    },
    # ── India test policies ──────────────────────────────────────────────────
    {
        "policy_number": "POL-COMP-IN-TEST",
        "holder_name": "Test Claimant",
        "type": "auto",
        "status": "active",
        "start_date": "2025-06-01",
        "end_date": "2027-06-01",
        "deductible": 2000,
        "coverage": {
            "comprehensive": 800000,
            "third_party": 1500000,
            "collision": 800000,
            "pa_cover": 1500000,
        },
        "exclusions": ["racing", "drunk_driving", "unlicensed_driver"],
        "premium_monthly": 3500,
        "claims_count": 0,
        "idv": 750000,
    },
    {
        "policy_number": "POL-TP-IN-TEST",
        "holder_name": "Test Claimant",
        "type": "auto",
        "status": "active",
        "start_date": "2025-06-01",
        "end_date": "2027-06-01",
        "deductible": 0,
        "coverage": {
            "third_party": 1500000,
            "pa_cover": 1500000,
        },
        "exclusions": ["drunk_driving"],
        "premium_monthly": 1200,
        "claims_count": 0,
    },
    # ── Update original sample policies with non-expired dates ───────────────
    {
        "policy_number": "POL-AUTO-789456",
        "holder_name": "Jane Smith",
        "type": "auto",
        "status": "active",
        "start_date": "2025-01-15",
        "end_date": "2027-01-15",
        "deductible": 1000,
        "coverage": {
            "collision": 50000,
            "comprehensive": 50000,
            "liability": 100000,
            "medical_payments": 5000,
            "rental_reimbursement": 1500,
        },
        "exclusions": ["racing", "commercial_use", "pre_existing_damage"],
        "premium_monthly": 127,
        "claims_count": 0,
    },
    {
        "policy_number": "POL-HOME-334521",
        "holder_name": "Robert Johnson",
        "type": "homeowners",
        "status": "active",
        "start_date": "2025-03-01",
        "end_date": "2027-03-01",
        "deductible": 2500,
        "coverage": {
            "dwelling": 350000,
            "personal_property": 100000,
            "liability": 300000,
            "additional_living": 50000,
        },
        "exclusions": ["flood", "earthquake", "intentional_damage"],
        "premium_monthly": 185,
        "claims_count": 1,
    },
    {
        "policy_number": "POL-AUTO-112233",
        "holder_name": "Maria Garcia",
        "type": "auto",
        "status": "active",
        "start_date": "2025-06-01",
        "end_date": "2027-06-01",
        "deductible": 500,
        "coverage": {
            "collision": 30000,
            "comprehensive": 30000,
            "liability": 75000,
        },
        "exclusions": ["racing"],
        "premium_monthly": 89,
        "claims_count": 2,
    },
]


def main():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS policies (
                policy_number TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
        """))
        for pol in POLICIES:
            conn.execute(
                text("INSERT INTO policies (policy_number, data) VALUES (:num, :data) "
                     "ON CONFLICT (policy_number) DO UPDATE SET data = :data"),
                {"num": pol["policy_number"], "data": json.dumps(pol)},
            )
        rows = conn.execute(text("SELECT policy_number FROM policies ORDER BY policy_number")).fetchall()

    print(f"\nSeeded {len(POLICIES)} policies into {DB_PATH}:\n")
    for r in rows:
        print(f"  {r[0]}")
    print(f"\nUS test policies:    POL-AUTO-TEST-US, POL-AUTO-HIGHDED-US, POL-HOME-TEST-US")
    print(f"India test policies: POL-COMP-IN-TEST, POL-TP-IN-TEST")
    print(f"Sample policies:     POL-AUTO-789456, POL-HOME-334521, POL-AUTO-112233 (dates updated to 2025-2027)\n")


if __name__ == "__main__":
    main()
