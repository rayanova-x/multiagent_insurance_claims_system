"""Tests for HITL checkpoint trigger logic."""

import pytest
from src.hitl.checkpoint import check_hitl_required
from src.models.schemas import FraudAssessmentOutput, FraudRiskLevel, HITLPriority, IntakeValidationOutput, ClaimType


def _make_intake(is_valid=True, flags=None):
    return IntakeValidationOutput(
        is_valid=is_valid,
        claim_type=ClaimType.AUTO_COLLISION,
        policy_active=True,
        claimant_eligible=True,
        missing_documents=[],
        intake_notes="OK",
        validation_flags=flags or [],
    )


def _make_fraud(score=0.20, risk=FraudRiskLevel.LOW):
    return FraudAssessmentOutput(
        fraud_risk_level=risk,
        fraud_score=score,
        primary_concerns=[],
        recommendation="proceed",
        crew_summary="Analysis complete",
        pattern_score=score,
        anomaly_score=score,
        consistency_score=0.90,
    )


def test_intake_llm_outage_always_triggers_hitl_regardless_of_amount():
    """An intake LLM failure (both primary and fallback model exhausted their
    retries, see src/llm.py) is a system failure, not a confidence judgment
    call - it must always route to a human, even on a small claim where the
    amount-based confidence gate would otherwise let it proceed untouched."""
    low_value_claim = {"claim_id": "CLM-900", "policy_number": "POL-001", "estimated_amount": 500, "is_appeal": False}
    intake = _make_intake(is_valid=True, flags=["LLM error - requires manual review"])
    req, triggers, priority, score = check_hitl_required(
        claim=low_value_claim,
        intake_output=intake,
        fraud_output=None,
    )
    assert req
    assert any("LLM unavailable" in t for t in triggers)


def test_disputed_incident_type_triggers_hitl_regardless_of_amount():
    """The claimant kept their own incident_type after /verify-type
    suggested a different one (src/security/incident_type_check.py) -
    neither side should silently win, so this must trigger review even on
    a small claim."""
    low_value_claim = {
        "claim_id": "CLM-901", "policy_number": "POL-001", "estimated_amount": 500, "is_appeal": False,
        "type_disputed_by_claimant": True, "type_dispute_reason": "I was definitely rear-ended, not robbed",
    }
    req, triggers, priority, score = check_hitl_required(
        claim=low_value_claim,
        intake_output=_make_intake(),
        fraud_output=None,
    )
    assert req
    assert any("disputed suggested incident_type" in t and "rear-ended" in t for t in triggers)


def test_undisputed_incident_type_does_not_trigger_hitl():
    claim = {
        "claim_id": "CLM-902", "policy_number": "POL-001", "estimated_amount": 500, "is_appeal": False,
        "type_disputed_by_claimant": False,
    }
    req, triggers, priority, score = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=None,
    )
    assert not any("disputed suggested incident_type" in t for t in triggers)


def test_no_hitl_low_value_clean():
    claim = {"claim_id": "CLM-001", "policy_number": "POL-001", "estimated_amount": 2000, "is_appeal": False}
    req, triggers, priority, score = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.10),
        damage_assessed_usd=2000,
        claim_history_count=0,
    )
    assert not req
    assert len(triggers) == 0


def test_hitl_high_value():
    claim = {"claim_id": "CLM-002", "policy_number": "POL-001", "estimated_amount": 15000, "is_appeal": False}
    req, triggers, priority, score = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.15),
        damage_assessed_usd=15000,
    )
    assert req
    assert any("$15,000" in t or "threshold" in t.lower() for t in triggers)


def test_hitl_high_fraud():
    claim = {"claim_id": "CLM-003", "policy_number": "POL-001", "estimated_amount": 5000, "is_appeal": False}
    req, triggers, priority, score = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.70, risk=FraudRiskLevel.HIGH),
        damage_assessed_usd=5000,
    )
    assert req
    assert any("fraud" in t.lower() for t in triggers)


def test_hitl_appeal_always():
    claim = {"claim_id": "CLM-004", "policy_number": "POL-001", "estimated_amount": 100, "is_appeal": True}
    req, triggers, priority, score = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.10),
    )
    assert req
    assert any("appeal" in t.lower() for t in triggers)


def test_priority_critical_high_value():
    # Appeal always forces CRITICAL
    claim = {"claim_id": "CLM-005", "policy_number": "POL-001", "estimated_amount": 100000, "is_appeal": True}
    req, triggers, priority, score = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.75, risk=FraudRiskLevel.HIGH),
        damage_assessed_usd=100000,
    )
    assert priority == HITLPriority.CRITICAL


def test_low_confidence_alone_does_not_trigger_hitl():
    """Confidence must never be a standalone HITL trigger - a confident
    hallucination would sail through a confidence-only gate, and routing
    low-value claims to review on self-doubt alone floods the queue with
    noise. Low confidence only raises priority once some other, property-
    based trigger already queued the claim (see test below)."""
    claim = {"claim_id": "CLM-006", "policy_number": "POL-001", "estimated_amount": 3000, "is_appeal": False}
    req, triggers, priority, score = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.20),
        agent_confidence_scores=[0.40, 0.45],  # Below the priority-score floor
    )
    assert not req
    assert not any("confidence" in t.lower() for t in triggers)


def test_low_confidence_raises_priority_of_an_already_triggered_claim():
    """Once a claim is queued for a property-based reason (here: high
    amount), low confidence should push its priority score up - this is the
    only role confidence plays."""
    claim = {"claim_id": "CLM-006a", "policy_number": "POL-001", "estimated_amount": 15000, "is_appeal": False}
    req_low_conf, _, _, score_low_conf = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.20),
        agent_confidence_scores=[0.40, 0.45],
    )
    req_high_conf, _, _, score_high_conf = check_hitl_required(
        claim=claim,
        intake_output=_make_intake(),
        fraud_output=_make_fraud(score=0.20),
        agent_confidence_scores=[0.95, 0.95],
    )
    assert req_low_conf and req_high_conf  # amount alone already triggers both
    assert score_low_conf > score_high_conf
