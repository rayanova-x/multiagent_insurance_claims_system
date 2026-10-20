"""The communication prompt told the model to write "[CLAIMANT]" as a
placeholder, promising the real name "will be merged separately" - but
nothing ever did that merge. Every generated letter literally contained the
text "[CLAIMANT]" instead of the claimant's name.
"""

from types import SimpleNamespace

import src.agents.communication_agent as ca
from src.models.schemas import ClaimDecision, CommunicationOutput


def _run(monkeypatch, claimant_name="Jordan Lee"):
    monkeypatch.setattr(ca, "log_agent_action", lambda **k: None)
    monkeypatch.setattr(ca, "log_final_decision", lambda **k: None)
    monkeypatch.setattr(ca, "run_versions_update", lambda *a, **k: {})
    reply = CommunicationOutput(
        subject="Claim Decision for [CLAIMANT]",
        message="Dear [CLAIMANT], your claim has been approved.",
        internal_notes="n/a",
        next_steps=["None"],
        appeal_instructions=None,
    )
    monkeypatch.setattr(ca, "get_structured_llm", lambda schema: SimpleNamespace(invoke=lambda msgs: reply))
    state = {
        "claim": {"claim_id": "CLM-C1", "policy_number": "P", "incident_type": "auto_collision",
                  "incident_date": "2026-01-01", "claimant_name": claimant_name},
        "settlement_output": None,
        "fraud_output": None,
        "evaluation_output": None,
        "hitl_required": False,
        "human_decision": None,
        "final_decision": ClaimDecision.APPROVED,
        "final_amount_usd": 1000.0,
    }
    return ca.run_communication_agent(state)["communication_output"]


def test_claimant_placeholder_is_replaced_with_the_real_name(monkeypatch):
    out = _run(monkeypatch, claimant_name="Jordan Lee")
    assert "[CLAIMANT]" not in out.subject
    assert "[CLAIMANT]" not in out.message
    assert "Jordan Lee" in out.subject
    assert "Jordan Lee" in out.message


def test_missing_claimant_name_falls_back_to_a_safe_default(monkeypatch):
    out = _run(monkeypatch, claimant_name="")
    assert "[CLAIMANT]" not in out.message
    assert "Claimant" in out.message


# ── Fraud-leak backstop: the prompt instruction is not a control ───────────
# (CLAUDE.md "structural fixes over prompt wording") - these test the code
# that scans claimant-facing text and replaces a leak with the safe
# template, in case the LLM ignores the "do not mention fraud" instruction.

from src.models.schemas import FraudRiskLevel


def _fraud(score=0.85):
    return SimpleNamespace(fraud_score=score, fraud_risk_level=FraudRiskLevel.HIGH)


def test_leaked_fraud_mention_is_replaced_with_the_safe_template():
    output = CommunicationOutput(
        subject="Claim Denied Due to Fraud Investigation",
        message="We regret to inform you that our fraud investigation found issues with your claim.",
        internal_notes="original notes",
        next_steps=["Contact your agent"],
        appeal_instructions=None,
    )
    ca._enforce_no_fraud_leak(output, "CLM-1", "denied", 0.0)
    assert "fraud" not in output.message.lower()
    assert "Dear Claimant" in output.message  # the fallback template's own text
    assert "SAFETY OVERRIDE" in output.internal_notes
    assert "original notes" in output.internal_notes  # original notes preserved, not discarded


def test_clean_message_is_left_untouched():
    output = CommunicationOutput(
        subject="Insurance Claim CLM-2 - Decision Notice",
        message="We regret to inform you that your claim has been denied based on our policy review.",
        internal_notes="original notes",
        next_steps=["Contact your agent"],
        appeal_instructions=None,
    )
    original_message = output.message
    ca._enforce_no_fraud_leak(output, "CLM-2", "denied", 0.0)
    assert output.message == original_message
    assert output.internal_notes == "original notes"


def test_investigation_term_is_also_caught():
    output = CommunicationOutput(
        subject="Claim Status",
        message="Your claim requires further investigation before a decision can be made.",
        internal_notes="notes",
        next_steps=[],
        appeal_instructions=None,
    )
    ca._enforce_no_fraud_leak(output, "CLM-3", "pending_documents", 0.0)
    assert "investigat" not in output.message.lower()


def test_enforcement_only_runs_when_fraud_score_is_elevated(monkeypatch):
    """Gated on fraud_output.fraud_score > 0.5 in run_communication_agent -
    verify it's not called at all for a clean claim."""
    monkeypatch.setattr(ca, "get_structured_llm", lambda schema: SimpleNamespace(
        invoke=lambda msgs: CommunicationOutput(
            subject="Claim Approved", message="Good news, your claim is approved.",
            internal_notes="n", next_steps=[], appeal_instructions=None,
        )
    ))
    monkeypatch.setattr(ca, "log_agent_action", lambda **k: None)
    monkeypatch.setattr(ca, "log_final_decision", lambda **k: None)
    monkeypatch.setattr(ca, "run_versions_update", lambda *a, **k: {})
    called = []
    monkeypatch.setattr(ca, "_enforce_no_fraud_leak", lambda *a, **k: called.append(True))

    state = {
        "claim": {"claim_id": "CLM-4", "claimant_name": "Jane", "policy_number": "P",
                  "incident_type": "auto_collision", "incident_date": "2026-01-01"},
        "settlement_output": None, "fraud_output": _fraud(score=0.1),  # low score
        "evaluation_output": None, "hitl_required": False, "human_decision": None,
        "final_decision": None, "final_amount_usd": 0.0,
    }
    ca.run_communication_agent(state)
    assert called == []


def test_enforcement_runs_when_fraud_score_is_elevated(monkeypatch):
    monkeypatch.setattr(ca, "get_structured_llm", lambda schema: SimpleNamespace(
        invoke=lambda msgs: CommunicationOutput(
            subject="Claim Denied", message="Your claim was denied.",
            internal_notes="n", next_steps=[], appeal_instructions=None,
        )
    ))
    monkeypatch.setattr(ca, "log_agent_action", lambda **k: None)
    monkeypatch.setattr(ca, "log_final_decision", lambda **k: None)
    monkeypatch.setattr(ca, "run_versions_update", lambda *a, **k: {})
    called = []
    monkeypatch.setattr(ca, "_enforce_no_fraud_leak", lambda *a, **k: called.append(True))

    state = {
        "claim": {"claim_id": "CLM-5", "claimant_name": "Jane", "policy_number": "P",
                  "incident_type": "auto_collision", "incident_date": "2026-01-01"},
        "settlement_output": None, "fraud_output": _fraud(score=0.85),  # elevated
        "evaluation_output": None, "hitl_required": False, "human_decision": None,
        "final_decision": None, "final_amount_usd": 0.0,
    }
    ca.run_communication_agent(state)
    assert called == [True]
