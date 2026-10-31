"""Ephemeral pre-claim document upload staging.

A real claim_id doesn't exist until /api/claims/submit creates the Claim DB
row (api/routes_claims.py), but the frontend uploads files immediately per
slot, before the claimant has finished the rest of the form. The frontend
generates a random upload_token when the form loads; every staged file for
that in-progress claim is filed under data/uploads/staging/{token}/, with a
manifest.json tracking what's there - file-based (not just an in-memory
dict) so a server restart between upload and submit doesn't lose it, same
"everything lives under data/" convention as this project's SQLite files.

Not full claim state: no DB row, no durability guarantees beyond "the files
are on disk." /api/claims/submit reads the manifest, moves the files under
the real claim_id, and the token is discarded.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Optional

_STAGING_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "uploads" / "staging"
_CLAIMED_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "uploads" / "claims"

ALLOWED_SLOTS = ("police_report", "other_driver_info", "repair_estimate", "other")
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB - generous for a single-page PDF, not unbounded


def _is_safe_token(token: str) -> bool:
    """Reject anything that isn't a plain token - this becomes a directory
    name, so path traversal (../, absolute paths) must never reach disk."""
    try:
        uuid.UUID(token)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _manifest_path(token: str) -> Path:
    return _STAGING_ROOT / token / "manifest.json"


def _read_manifest(token: str) -> dict:
    path = _manifest_path(token)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_manifest(token: str, manifest: dict) -> None:
    path = _manifest_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))


def save_staged_file(token: str, slot: str, filename: str, content: bytes) -> Path:
    """Write the uploaded file to disk, overwriting any previous upload for
    this token+slot (re-upload flow). Raises ValueError on an invalid token,
    slot, or oversized file - callers map this to a 4xx at the API boundary."""
    if not _is_safe_token(token):
        raise ValueError("Invalid upload token")
    if slot not in ALLOWED_SLOTS:
        raise ValueError(f"Invalid slot: {slot}")
    if len(content) > _MAX_FILE_BYTES:
        raise ValueError("File too large")
    if not content.startswith(b"%PDF"):
        raise ValueError("Only PDF files are accepted")

    slot_dir = _STAGING_ROOT / token
    slot_dir.mkdir(parents=True, exist_ok=True)
    file_path = slot_dir / f"{slot}.pdf"
    file_path.write_bytes(content)
    return file_path


def record_staged_result(token: str, slot: str, path: Path, status: str, message: str, finding: Optional[dict]) -> None:
    manifest = _read_manifest(token)
    manifest[slot] = {
        "path": str(path),
        "original_filename": None,  # set by the caller if it wants to track it; not required downstream
        "status": status,
        "message": message,
        "finding": finding,
    }
    _write_manifest(token, manifest)


def remove_staged_file(token: str, slot: str) -> None:
    if not _is_safe_token(token) or slot not in ALLOWED_SLOTS:
        raise ValueError("Invalid token or slot")
    manifest = _read_manifest(token)
    manifest.pop(slot, None)
    _write_manifest(token, manifest)
    file_path = _STAGING_ROOT / token / f"{slot}.pdf"
    file_path.unlink(missing_ok=True)


def get_staged_manifest(token: str) -> dict:
    if not _is_safe_token(token):
        return {}
    return _read_manifest(token)


def claim_staged_documents(token: str, claim_id: str) -> dict:
    """Move staged files from the throwaway token directory to permanent,
    claim_id-scoped storage. Returns the uploaded_documents dict shape
    ClaimsState already expects: {"police_report": {"path": "..."}, ...}
    (the 3 required slots only - "other" files are moved too but not
    included in this dict, since document_agent never runs structured
    extraction on them - see the original document-agent design)."""
    manifest = get_staged_manifest(token)
    if not manifest:
        return {}

    dest_dir = _CLAIMED_ROOT / claim_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    uploaded_documents: dict = {}
    for slot, entry in manifest.items():
        src = Path(entry["path"])
        if not src.exists():
            continue
        dest = dest_dir / f"{slot}.pdf"
        shutil.move(str(src), str(dest))
        if slot != "other":
            uploaded_documents[slot] = {"path": str(dest)}

    # Clean up the now-empty staging directory for this token.
    shutil.rmtree(_STAGING_ROOT / token, ignore_errors=True)

    return uploaded_documents
