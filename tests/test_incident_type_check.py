"""src/security/incident_type_check.py: a narrow classifier comparing the
claimant's selected incident_type against what the description describes.
Mirrors tests/test_injection_check.py's shape for the sibling classifier."""

from types import SimpleNamespace

import src.security.incident_type_check as itc
from src.models.schemas import ClaimType


def _llm(result=None, raises=None):
    def invoke(msgs):
        if raises:
            raise raises
        return result
    return SimpleNamespace(invoke=invoke)


def test_too_short_description_returns_none_without_calling_the_llm(monkeypatch):
    monkeypatch.setattr(itc, "get_structured_llm", lambda schema: (_ for _ in ()).throw(
        AssertionError("should not be called")))
    assert itc.check_incident_type("hit", "auto_collision") is None


def test_empty_description_returns_none():
    assert itc.check_incident_type("", "auto_collision") is None


def test_matching_classification_is_returned(monkeypatch):
    result = itc.IncidentTypeCheckOutput(
        classified_type=ClaimType.AUTO_COLLISION, reasoning="Describes a rear-end crash.", confidence=0.9,
    )
    monkeypatch.setattr(itc, "get_structured_llm", lambda schema: _llm(result))
    out = itc.check_incident_type("I was rear-ended at a red light", "auto_collision")
    assert out.classified_type == ClaimType.AUTO_COLLISION


def test_mismatched_classification_is_returned_for_caller_to_compare(monkeypatch):
    result = itc.IncidentTypeCheckOutput(
        classified_type=ClaimType.AUTO_THEFT,
        reasoning="Vehicle was missing from where it was parked, not a collision.",
        confidence=0.85,
    )
    monkeypatch.setattr(itc, "get_structured_llm", lambda schema: _llm(result))
    out = itc.check_incident_type("My car was gone when I came back to the lot", "auto_collision")
    assert out.classified_type == ClaimType.AUTO_THEFT
    assert "missing" in out.reasoning


def test_low_confidence_unknown_returns_none_not_a_false_mismatch(monkeypatch):
    """An UNKNOWN classification the model itself isn't confident in should
    not contradict the claimant with a guess - returning it as a 'mismatch'
    against any real selected type would be worse than saying nothing."""
    result = itc.IncidentTypeCheckOutput(
        classified_type=ClaimType.UNKNOWN, reasoning="Too vague to classify.", confidence=0.3,
    )
    monkeypatch.setattr(itc, "get_structured_llm", lambda schema: _llm(result))
    out = itc.check_incident_type("something happened to my stuff", "auto_collision")
    assert out is None


def test_high_confidence_unknown_is_still_returned(monkeypatch):
    """A confident UNKNOWN (model is sure this ISN'T any known type) is
    still useful signal, unlike a low-confidence guess."""
    result = itc.IncidentTypeCheckOutput(
        classified_type=ClaimType.UNKNOWN, reasoning="Describes a dispute with a neighbor, not a loss event.",
        confidence=0.8,
    )
    monkeypatch.setattr(itc, "get_structured_llm", lambda schema: _llm(result))
    out = itc.check_incident_type("My neighbor and I disagree about our fence line", "auto_collision")
    assert out is not None
    assert out.classified_type == ClaimType.UNKNOWN


def test_llm_failure_fails_open_returns_none(monkeypatch):
    """Unlike injection_check (fails closed, security concern), a classifier
    outage here should not block submission - just skip verification."""
    monkeypatch.setattr(itc, "get_structured_llm", lambda schema: _llm(raises=RuntimeError("provider down")))
    out = itc.check_incident_type("I was rear-ended at a red light", "auto_collision")
    assert out is None
