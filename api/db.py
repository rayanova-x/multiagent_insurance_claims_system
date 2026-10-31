"""Persistence layer via SQLModel.

Users/claims/appeals. Uses Postgres when DATABASE_URL is set (Render injects
this automatically when a Postgres instance is attached to the web service -
Render's own disk is ephemeral, so SQLite would lose everything on every
redeploy/restart). Falls back to a local SQLite file when DATABASE_URL is
unset, so local dev needs no Postgres install."""

from __future__ import annotations #For Python 3.10+ to allow forward references in type hints without quotes Ensure this is at the top of the file before any imports or code.

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlmodel import Field, Session, SQLModel, create_engine

load_dotenv()  # must run before reading DATABASE_URL below - don't rely on another module having loaded .env first

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _PROJECT_ROOT / "data" / "api.db"  # SQLite fallback path only; unused when DATABASE_URL is set

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    engine = create_engine(DATABASE_URL)
else:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    role: str = "user"  # user | reviewer | admin
    created_at: str = Field(default_factory=_utcnow)


class Claim(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    claim_id: str = Field(index=True, unique=True)
    user_id: Optional[int] = Field(default=None, index=True)
    policy_number: str
    claimant_name: str = ""
    claimant_email: str = ""
    claimant_phone: str = ""
    claimant_dob: str = ""
    incident_date: str = ""
    incident_type: str = ""
    incident_description: str = ""
    incident_location: str = ""
    police_report_number: Optional[str] = None
    estimated_amount: float = 0.0
    vehicle_year: Optional[int] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    documents: str = "[]"  # JSON list
    type_disputed_by_claimant: bool = False  # claimant kept their own incident_type after the verify-type check suggested a different one
    type_dispute_reason: Optional[str] = None  # claimant's own stated justification, shown to the HITL reviewer alongside the model's reasoning
    document_disputes: str = "[]"  # JSON list of {"slot": ..., "reason": ...} - claimant kept a flagged document anyway, see upload_staging.py
    upload_token: Optional[str] = None  # pre-claim upload session token (api/routes_claims.py's /upload-staging/{token}/{slot}), used once to claim staged files, not read again after
    prebuilt_documents: Optional[str] = None  # JSON AutoCollisionDocuments dict - only set by the Evals tab's golden-set "Run this case" flow, see routes_evals.py

    status: str = "submitted"  # submitted | processing | completed | failed
    final_decision: Optional[str] = None
    settlement_amount: Optional[float] = None
    fraud_score: Optional[float] = None
    fraud_risk_level: Optional[str] = None
    evaluation_score: Optional[float] = None
    decided_by: Optional[str] = None      # "AI Agent" or reviewer username
    decided_at: Optional[str] = None      # ISO timestamp of final decision
    hitl_required: bool = False
    hitl_ticket_id: Optional[str] = None
    agent_call_count: int = 0
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    processing_time_sec: float = 0.0
    denial_reasons: str = "[]"  # JSON list
    communication_message: Optional[str] = None
    appeal_instructions: Optional[str] = None
    agent_outputs: Optional[str] = None  # JSON: serialized per-agent Pydantic outputs
    pipeline_path: str = ""  # JSON list of agents called
    error_log: str = "[]"  # JSON list

    created_at: str = Field(default_factory=_utcnow)
    completed_at: Optional[str] = None


class Appeal(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    appeal_id: str = Field(index=True, unique=True)
    claim_id: str = Field(index=True)
    user_id: Optional[int] = None
    reason: str = ""
    status: str = "pending"  # pending | approved | denied
    reviewer_id: Optional[int] = None
    review_decision: Optional[str] = None
    review_notes: Optional[str] = None
    created_at: str = Field(default_factory=_utcnow)
    reviewed_at: Optional[str] = None


def _migrate_claim_columns() -> None:
    """Add new columns to existing claim table if missing.

    Uses SQLAlchemy's own dialect-agnostic inspector instead of SQLite's
    PRAGMA table_info, so this works unchanged against Postgres too - no
    per-dialect branching needed."""
    from sqlalchemy import inspect, text

    try:
        existing = {col["name"] for col in inspect(engine).get_columns("claim")}
    except Exception:
        return  # table may not exist yet

    migrations = [
        ("denial_reasons", "TEXT DEFAULT '[]'"),
        ("communication_message", "TEXT"),
        ("appeal_instructions", "TEXT"),
        ("agent_outputs", "TEXT"),
        ("decided_by", "TEXT"),
        ("decided_at", "TEXT"),
        ("type_disputed_by_claimant", "BOOLEAN DEFAULT FALSE"),
        ("type_dispute_reason", "TEXT"),
        ("document_disputes", "TEXT DEFAULT '[]'"),
        ("upload_token", "TEXT"),
        ("prebuilt_documents", "TEXT"),
    ]
    with engine.begin() as conn:
        for col, typedef in migrations:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE claim ADD COLUMN {col} {typedef}"))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_claim_columns()


def get_session():
    with Session(engine) as session:
        yield session
