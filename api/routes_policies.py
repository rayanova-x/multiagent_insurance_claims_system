"""
Policy Management - CRUD for the policies DB used by the claims pipeline.
Shares src/tools/policy_lookup.py's engine (Postgres when DATABASE_URL is
set, else local SQLite) - one connection abstraction, not a second one.

GET    /api/policies/        - List all policies (any authenticated user).
POST   /api/policies/        - Create / update a policy (admin only).
DELETE /api/policies/{num}   - Delete a policy (admin only).
"""
from __future__ import annotations # for Python 3.10-3.11 compatibility, allows forward references in type hints without quotes

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from api.security import get_current_user, require_role
from src.config import get_country_config, get_country_meta
from src.tools.policy_lookup import _ensure_db, _get_engine

router = APIRouter(prefix="/api/policies", tags=["Policies"])


# --- Country-aware default policy templates ---------------------------------

_US_DEFAULTS = [
    {
        "label": "US Auto (Comprehensive)",
        "policy_number": "POL-AUTO-",
        "holder_name": "",
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
        "label": "US Homeowners",
        "policy_number": "POL-HOME-",
        "holder_name": "",
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
]

_INDIA_DEFAULTS = [
    {
        "label": "India Comprehensive (Own Damage + TP)",
        "policy_number": "POL-COMP-IN-",
        "holder_name": "",
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
        "label": "India Third-Party Only",
        "policy_number": "POL-TP-IN-",
        "holder_name": "",
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
]


# --- Models ---------------------------------------------------------------

class PolicyCreate(BaseModel):
    policy_number: str = Field(..., min_length=1, examples=["POL-AUTO-001"])
    holder_name: str = Field(..., min_length=1)
    type: str = Field(..., examples=["auto", "homeowners"])
    status: str = Field(default="active", examples=["active", "lapsed"])
    start_date: str = Field(..., examples=["2025-01-01"])
    end_date: str = Field(..., examples=["2027-01-01"])
    deductible: float = 0
    coverage: Dict[str, Any] = Field(default_factory=dict)
    exclusions: List[str] = Field(default_factory=list)
    premium_monthly: float = 0
    claims_count: int = 0


# --- Endpoints --------------------------------------------------------─

@router.get("/defaults")
def get_policy_defaults(_=Depends(get_current_user)):
    """Return country-aware default policy templates for quick-create."""

    meta = get_country_meta()
    code = (meta.get("code") or "US").upper()
    templates = _INDIA_DEFAULTS if code == "IN" else _US_DEFAULTS
    return {
        "country": meta,
        "templates": templates,
    }


@router.get("/")
def list_policies(user=Depends(get_current_user)):
    """Return policies, filtered by active country and current user."""

    _ensure_db()
    meta = get_country_meta()
    country_code = (meta.get("code") or "US").upper()
    with _get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT policy_number, data FROM policies ORDER BY policy_number")
        ).fetchall()
    policies = [json.loads(row[1]) for row in rows]
    # Filter: India policies contain "-IN-" in policy number; others are US
    if country_code == "IN":
        policies = [p for p in policies if "-IN-" in p.get("policy_number", "")]
    else:
        policies = [p for p in policies if "-IN-" not in p.get("policy_number", "")]
    # Non-admin users only see their own policies (matched by holder_name)
    if user.role not in ("admin", "reviewer"):
        username = user.username
        policies = [p for p in policies if p.get("holder_name", "").lower() == username.lower()]
    return policies


@router.post("/")
def upsert_policy(body: PolicyCreate, _=Depends(get_current_user)):
    """Create or update a policy."""

    _ensure_db()
    data = body.model_dump()
    with _get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO policies (policy_number, data) VALUES (:num, :data) "
                 "ON CONFLICT (policy_number) DO UPDATE SET data = :data"),
            {"num": data["policy_number"], "data": json.dumps(data)},
        )
    return {"ok": True, "policy_number": data["policy_number"]}


@router.delete("/{policy_number}")
def delete_policy(policy_number: str, _=Depends(require_role("admin"))):
    """Delete a policy by its policy_number."""

    _ensure_db()
    with _get_engine().begin() as conn:
        result = conn.execute(
            text("DELETE FROM policies WHERE policy_number = :num"), {"num": policy_number}
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Policy '{policy_number}' not found")
    return {"ok": True, "deleted": policy_number}
