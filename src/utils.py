"""
Shared utilities used across agents and tools.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def currency_symbol() -> str:
    """Return the currency symbol for the active country config."""
    try:
        from src.config import get_country_meta
        return get_country_meta().get("currency_symbol", "$")
    except Exception:
        return "$"


_AUTO_KEYWORDS = {
    "auto", "vehicle", "car", "collision", "own_damage", "own damage",
    "third_party", "third party", "theft", "comprehensive", "motor",
}


def detect_asset_type(incident_type: str) -> str:
    """Return 'auto' or 'property' based on the incident type string."""
    it = incident_type.lower().replace("_", " ")
    return "auto" if any(k in it for k in _AUTO_KEYWORDS) else "property"


def year_of(as_of) -> int:
    """Calendar year of `as_of` (a date or ISO date string); today's year if absent or unparseable."""
    from datetime import date
    try:
        return (as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of))).year
    except (TypeError, ValueError):
        return date.today().year


def calculate_asset_age(vehicle_year: int | str | None, as_of=None) -> int:
    """Return asset age in years from the vehicle year. Returns 0 if unknown.

    `as_of` is the date the age is measured at - callers pass the claim's incident date,
    because the car's value at the time of loss is what is insured, not its value on the
    day the claim happens to be processed. Defaults to today for callers with no incident.
    """
    if not vehicle_year:
        return 0
    return year_of(as_of) - int(vehicle_year)


def recall_similar_claims(description: str, k: int = 3) -> str:
    """Retrieve similar past claims from memory. Returns formatted context string or empty string."""
    try:
        from src.memory.manager import memory
        similar = memory.recall_similar_claims(description, k=k)
        if not similar:
            return ""
        # Record what was read, so the decision record can name the precedents
        # this run saw. Memory grows daily and the same query returns different
        # neighbours next month (src/provenance.py).
        from src.provenance import record_retrieval
        record_retrieval(s["claim_id"] for s in similar)
        lines = ["SIMILAR PAST CLAIMS (from long-term memory):"]
        for s in similar:
            m = s.get("metadata", {})
            lines.append(
                f"  - {s['claim_id']}: decision={m.get('decision', '?')}, "
                f"settlement={m.get('settlement_amount', '?')}, "
                f"fraud_score={m.get('fraud_score', '?')}"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Memory recall skipped: %s", e)
        return ""
