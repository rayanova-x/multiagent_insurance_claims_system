"""
Clean claims data while preserving users and policies.

Usage:
    python scripts/clean_data.py              # Clean claims, appeals, HITL queue
    python scripts/clean_data.py --all        # Delete ALL DBs (full reset, loses users too)
    python scripts/clean_data.py --dry-run    # Show what would be deleted without doing it

What gets cleaned (default):
    - All claims (claim table in api.db)
    - All appeals (appeal table in api.db)
    - HITL queue (hitl_queue.db)
    - LangGraph checkpoints (claims_checkpoints.db)
    - Audit logs (data/audit_logs/)

What is preserved (default):
    - Users (user table in api.db)
    - Policies (policies.db)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from api.db import engine as api_engine  # noqa: E402
from src.hitl.queue import _get_engine as _get_hitl_engine  # noqa: E402
from src.tools.policy_lookup import _get_engine as _get_policies_engine  # noqa: E402

hitl_engine = _get_hitl_engine()
policies_engine = _get_policies_engine()

DATA_DIR = PROJECT_ROOT / "data"
# File-delete fallback only applies to the local SQLite files - unused when
# DATABASE_URL points at Postgres (there's no file to delete there).
API_DB = DATA_DIR / "api.db"
HITL_DB = DATA_DIR / "hitl_queue.db"
POLICIES_DB = DATA_DIR / "policies.db"
CHECKPOINTS_DB = DATA_DIR / "claims_checkpoints.db"
AUDIT_DIR = DATA_DIR / "audit_logs"
MEMORY_DIR = DATA_DIR / "memory"

_USING_POSTGRES = bool(os.getenv("DATABASE_URL"))


def _delete_file(path: Path, dry_run: bool) -> None:
    if path.exists():
        if dry_run:
            print(f"  [dry-run] Would delete: {path}")
        else:
            path.unlink()
            print(f"  Deleted: {path}")


def clean_claims_only(dry_run: bool = False) -> None:
    """Delete claims + appeals from the claim/appeal tables, keep users intact."""
    if not _USING_POSTGRES and not API_DB.exists():
        print("  api.db not found - nothing to clean")
        return

    with api_engine.connect() as conn:
        try:
            claims = conn.execute(text("SELECT COUNT(*) FROM claim")).scalar()
            appeals = conn.execute(text("SELECT COUNT(*) FROM appeal")).scalar()
            users = conn.execute(text("SELECT COUNT(*) FROM \"user\"")).scalar()
        except Exception:
            print("  claim/appeal/user tables not found - nothing to clean")
            return

    if dry_run:
        print(f"  [dry-run] Would delete {claims} claims, {appeals} appeals (keeping {users} users)")
        return

    with api_engine.begin() as conn:
        conn.execute(text("DELETE FROM claim"))
        conn.execute(text("DELETE FROM appeal"))
    print(f"  Cleared claims + appeals")


def clean_all(dry_run: bool = False) -> None:
    """Delete ALL databases - full reset."""
    if _USING_POSTGRES:
        for name, conn_engine in [("policies", policies_engine), ("hitl_queue", hitl_engine)]:
            if dry_run:
                print(f"  [dry-run] Would drop all rows in Postgres tables for: {name}")
                continue
            with conn_engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS policies"))
                conn.execute(text("DROP TABLE IF EXISTS hitl_queue"))
        if dry_run:
            print("  [dry-run] Would drop all rows in Postgres tables: claim, appeal, user")
        else:
            with api_engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS claim"))
                conn.execute(text("DROP TABLE IF EXISTS appeal"))
                conn.execute(text('DROP TABLE IF EXISTS "user"'))
        return
    for db in [API_DB, HITL_DB, POLICIES_DB, CHECKPOINTS_DB]:
        _delete_file(db, dry_run)


def clean_supporting(dry_run: bool = False) -> None:
    """Delete HITL queue, checkpoints, audit logs."""
    if _USING_POSTGRES:
        if dry_run:
            print("  [dry-run] Would drop Postgres tables: hitl_queue, checkpoints, checkpoint_writes, checkpoint_blobs")
        else:
            with hitl_engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS hitl_queue"))
            with api_engine.begin() as conn:
                for t in ("checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"):
                    conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
    else:
        _delete_file(HITL_DB, dry_run)
        _delete_file(CHECKPOINTS_DB, dry_run)

    if AUDIT_DIR.exists():
        if dry_run:
            count = sum(1 for _ in AUDIT_DIR.rglob("*") if _.is_file())
            print(f"  [dry-run] Would delete {count} audit log files in {AUDIT_DIR}")
        else:
            shutil.rmtree(AUDIT_DIR, ignore_errors=True)
            AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            print(f"  Cleared audit logs: {AUDIT_DIR}")

    if MEMORY_DIR.exists():
        if dry_run:
            count = sum(1 for _ in MEMORY_DIR.rglob("*") if _.is_file())
            print(f"  [dry-run] Would delete {count} memory files in {MEMORY_DIR}")
        else:
            shutil.rmtree(MEMORY_DIR, ignore_errors=True)
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            print(f"  Cleared memory store: {MEMORY_DIR}")


def main():
    parser = argparse.ArgumentParser(description="Clean Smart Claims Processor data")
    parser.add_argument("--all", action="store_true", help="Delete ALL DBs including users and policies (full reset)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without doing it")
    args = parser.parse_args()

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Smart Claims Data Cleanup")
    print("=" * 50)

    if args.all:
        print("\nMode: FULL RESET (deleting everything)")
        clean_all(args.dry_run)
        clean_supporting(args.dry_run)
    else:
        print("\nMode: Clean claims data (preserving users + policies)")
        print("\n1. Claims & Appeals:")
        clean_claims_only(args.dry_run)
        print("\n2. Supporting data:")
        clean_supporting(args.dry_run)

    print("\nDone!" + (" (dry run - no changes made)" if args.dry_run else ""))
    if not args.dry_run and not args.all:
        print("Tip: Restart the server to re-initialize tables.\n")


if __name__ == "__main__":
    main()
