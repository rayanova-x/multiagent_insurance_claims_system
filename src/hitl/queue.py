"""
HITL (Human-In-The-Loop) Review Queue - storage and queue operations.

Architecture:
  - SQLite-backed queue (no extra services required)
  - HTTP endpoints live in api/routes_hitl.py (mounted at /api/hitl)
  - Priority ordering: CRITICAL > HIGH > NORMAL
  - SLA tracking: a pending ticket past its deadline is escalated to CRITICAL
    and the escalation is written to the audit log
  - Idempotent enqueue: LangGraph re-runs a node from its first line when the
    graph resumes after interrupt(), so the node that creates a ticket runs
    twice for one pause. The idempotency key makes the second call return the
    ticket the first call created instead of opening a duplicate.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Row

from src.config import get_hitl_config
from src.models.schemas import HITLPriority
from src.security.audit_log import log_hitl_event

logger = logging.getLogger(__name__)

DB_PATH = Path("./data/hitl_queue.db")  # SQLite fallback path only; unused when DATABASE_URL is set

# ── Priority ordering for queue ───────────────────────────────────────────────
_PRIORITY_ORDER = {
    HITLPriority.CRITICAL.value: 0,
    HITLPriority.HIGH.value: 1,
    HITLPriority.NORMAL.value: 2,
}

_engine_cache: dict = {}   # keyed by (database_url, str(DB_PATH)) so tests that monkeypatch DB_PATH get a fresh engine
_migrated: set = set()     # same key - table/migration only needs to run once per engine


def _get_engine():
    """Builds (or reuses) the engine for the CURRENT DATABASE_URL/DB_PATH.

    Read fresh on every call, like the old sqlite3.connect(str(DB_PATH)) -
    this is what lets tests monkeypatch queue.DB_PATH and have it take
    effect, and lets DATABASE_URL be read even if set after import."""
    database_url = os.getenv("DATABASE_URL")
    key = (database_url, str(DB_PATH))
    if key not in _engine_cache:
        if database_url:
            _engine_cache[key] = create_engine(database_url)
        else:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _engine_cache[key] = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    return _engine_cache[key]


# ── Database Setup ────────────────────────────────────────────────────────────

def _ensure_db(engine) -> None:
    """Create the table + run in-place column migrations (once per engine)."""
    key = str(engine.url)
    if key in _migrated:
        return
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS hitl_queue (
                ticket_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                priority TEXT NOT NULL,
                priority_score REAL NOT NULL,
                triggers TEXT NOT NULL,
                review_brief TEXT NOT NULL,
                state_snapshot TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sla_deadline TEXT NOT NULL,
                resolved_at TEXT,
                reviewer_id TEXT,
                human_decision TEXT,
                human_notes TEXT,
                override_ai INTEGER DEFAULT 0
            )
        """))
        # Columns added after the first release: migrate existing databases
        # in place. Dialect-agnostic inspector instead of SQLite's PRAGMA.
        existing = {col["name"] for col in inspect(engine).get_columns("hitl_queue")}
        if "idempotency_key" not in existing:
            conn.execute(text("ALTER TABLE hitl_queue ADD COLUMN idempotency_key TEXT"))
        if "sla_breached" not in existing:
            conn.execute(text("ALTER TABLE hitl_queue ADD COLUMN sla_breached INTEGER DEFAULT 0"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hitl_idempotency "
            "ON hitl_queue(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
    _migrated.add(key)


def _get_db():
    """Returns an open SQLAlchemy connection against the CURRENT
    DATABASE_URL/DB_PATH. Caller must close() it (same contract the old
    sqlite3.Connection had) - rows support ["col"] access via row._mapping,
    not directly (see callers)."""
    engine = _get_engine()
    _ensure_db(engine)
    return engine.connect()


# ── Queue Operations ──────────────────────────────────────────────────────────

def enqueue_claim(
    claim_id: str,
    priority: HITLPriority,
    priority_score: float,
    triggers: list[str],
    review_brief: str,
    state_snapshot: dict,
    idempotency_key: Optional[str] = None,
) -> str:
    """Add a claim to the HITL review queue. Returns ticket_id.

    With an idempotency_key, calling this again for the same pause returns the
    existing ticket and writes nothing.
    """
    conn = _get_db()
    try:
        if idempotency_key:
            row = conn.execute(
                text("SELECT ticket_id FROM hitl_queue WHERE idempotency_key = :k"),
                {"k": idempotency_key},
            ).fetchone()
            if row:
                logger.info(f"HITL ticket {row[0]} already exists for {idempotency_key}; reusing")
                return row[0]

        cfg = get_hitl_config()
        sla_hours = cfg["sla_hours"].get(priority.value, 72)
        ticket_id = f"HITL-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(timezone.utc)
        sla_deadline = now + timedelta(hours=sla_hours)

        conn.execute(text("""
            INSERT INTO hitl_queue
            (ticket_id, claim_id, priority, priority_score, triggers, review_brief,
             state_snapshot, status, created_at, sla_deadline, idempotency_key)
            VALUES (:ticket_id, :claim_id, :priority, :priority_score, :triggers, :review_brief,
                    :state_snapshot, 'pending', :created_at, :sla_deadline, :idempotency_key)
        """), {
            "ticket_id": ticket_id,
            "claim_id": claim_id,
            "priority": priority.value,
            "priority_score": priority_score,
            "triggers": json.dumps(triggers),
            "review_brief": review_brief,
            "state_snapshot": json.dumps(state_snapshot, default=str),
            "created_at": now.isoformat(),
            "sla_deadline": sla_deadline.isoformat(),
            "idempotency_key": idempotency_key,
        })
        conn.commit()
    finally:
        conn.close()

    log_hitl_event(
        claim_id=claim_id,
        event="ENQUEUED",
        priority=priority.value,
        triggers=triggers,
    )

    logger.info(f"HITL ticket {ticket_id} created for claim {claim_id} | Priority: {priority.value}")
    return ticket_id


def escalate_overdue_tickets(conn, now: Optional[datetime] = None) -> list[str]:
    """Escalate pending tickets past their SLA deadline to CRITICAL.

    Called whenever the queue is read, so a reviewer opening the queue always
    sees overdue work at the top. Each escalation happens once per ticket and
    is written to the audit log. Returns the escalated ticket ids.

    conn: an open SQLAlchemy connection (from _get_db()) - caller owns its
    lifecycle, same contract the old sqlite3.Connection parameter had.
    """
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        text("SELECT ticket_id, claim_id, priority, triggers, sla_deadline FROM hitl_queue "
             "WHERE status = 'pending' AND COALESCE(sla_breached, 0) = 0 AND sla_deadline < :now"),
        {"now": now.isoformat()},
    ).fetchall()
    escalated = []
    for row in rows:
        r = row._mapping
        conn.execute(
            text("UPDATE hitl_queue SET sla_breached = 1, priority = :priority, "
                 "priority_score = CASE WHEN priority_score > 80 THEN priority_score ELSE 80 END "
                 "WHERE ticket_id = :ticket_id"),
            {"priority": HITLPriority.CRITICAL.value, "ticket_id": r["ticket_id"]},
        )
        log_hitl_event(
            claim_id=r["claim_id"],
            event="SLA_BREACHED_ESCALATED",
            priority=HITLPriority.CRITICAL.value,
            triggers=[f"SLA deadline {r['sla_deadline']} passed while pending "
                      f"(was {r['priority']})"],
        )
        escalated.append(r["ticket_id"])
    if escalated:
        conn.commit()
        logger.warning(f"Escalated {len(escalated)} overdue HITL ticket(s) to critical: {escalated}")
    return escalated


def get_human_decision(ticket_id: str) -> Optional[dict]:
    """Return the recorded human decision for a ticket, or None if still pending."""
    conn = _get_db()
    try:
        row = conn.execute(
            text("SELECT * FROM hitl_queue WHERE ticket_id = :t"), {"t": ticket_id}
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None
    r = row._mapping
    if r["status"] != "resolved":
        return None

    return {
        "decision": r["human_decision"],
        "reviewer_id": r["reviewer_id"],
        "notes": r["human_notes"],
        "override_ai": bool(r["override_ai"]),
        "resolved_at": r["resolved_at"],
    }
