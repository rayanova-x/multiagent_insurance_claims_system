"""Shared harness for tests/test_scenarios_e2e.py.

Not a test file itself (no test_ functions) - a toolkit for driving the
REAL compiled LangGraph graph (src.agents.graph.process_claim /
resume_claim) with every LLM-calling agent mocked out, then reading the
resulting pipeline_path/final_decision/state back for assertions.

This is deliberately a different layer than tests/test_graph_routing.py:
that file unit-tests each router FUNCTION in isolation with hand-built
SimpleNamespace state. This harness instead proves the WIRING - that
LangGraph's actual state-merge/fan-out/retry-loop engine, running for
real, produces the right end-to-end behavior when agents return given
outputs. This is the only test layer that would have caught the real
`consistency_retry_context` concurrent-write bug found this session -
unit tests of the node functions alone did not exercise LangGraph's
concurrent super-step merge at all.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver

from src.agents import graph as g
from src.models.schemas import (
    ClaimDecision,
    ClaimType,
    CommunicationOutput,
    ConflictFinding,
    ConflictSeverity,
    ConflictType,
    ConsistencyCheckOutput,
    CoverageStatus,
    DamageAssessmentOutput,
    EvaluationOutput,
    FraudAssessmentOutput,
    FraudRiskLevel,
    IntakeValidationOutput,
    JudgeDimensionOutput,
    PolicyCheckOutput,
    SettlementOutput,
)
from src.models.state import ClaimInput

# ── Default, "everything is fine" output builders ──────────────────────────
# Each returns a real Pydantic schema instance (not a SimpleNamespace) -
# agents' own code sometimes reads enum .value, so these must behave exactly
# like the real LLM-returned objects they stand in for.


def make_intake(is_valid=True, claim_type=ClaimType.AUTO_COLLISION, flags=None):
    return IntakeValidationOutput(
        is_valid=is_valid, claim_type=claim_type, policy_active=is_valid,
        claimant_eligible=is_valid, missing_documents=[], intake_notes="ok",
        validation_flags=flags or [],
    )


def make_damage(amount=9000.0, recommendation="repair", confidence=0.90,
                 requires_inspection=False, notes="ok"):
    return DamageAssessmentOutput(
        assessed_damage_usd=amount, line_items=[], repair_vs_replace=recommendation,
        assessment_confidence=confidence, assessment_notes=notes,
        requires_physical_inspection=requires_inspection, comparable_claims_avg=None,
    )


def make_policy(status=CoverageStatus.COVERED, exclusions=None, compliance_flags=None,
                 confidence=0.90, deductible=1000.0):
    return PolicyCheckOutput(
        coverage_status=status, covered_amount_usd=0.0, deductible_usd=deductible,
        exclusions_triggered=exclusions or [], coverage_notes="ok",
        compliance_flags=compliance_flags or [], policy_limits={}, confidence=confidence,
    )


def make_fraud(score=0.1, level=FraudRiskLevel.LOW, confidence=0.90, summary="minor fender bender"):
    return FraudAssessmentOutput(
        fraud_risk_level=level, fraud_score=score, primary_concerns=[],
        recommendation="proceed", crew_summary=summary, pattern_score=score,
        anomaly_score=score, consistency_score=1.0 - score, narrative_risk=score,
        confidence=confidence,
    )


def make_consistency(has_conflicts=False, conflicts=None, summary="ok"):
    return ConsistencyCheckOutput(has_conflicts=has_conflicts, conflicts=conflicts or [], summary=summary)


def make_conflict(conflict_type=ConflictType.MISSED_FACT, severity=ConflictSeverity.MATERIAL,
                   retry_target="policy_checker", description="d", reasoning="r"):
    return ConflictFinding(
        conflict_type=conflict_type, severity=severity, description=description,
        reasoning=reasoning, retry_target=retry_target,
    )


def make_settlement(decision=ClaimDecision.APPROVED, amount=8000.0, gross=9000.0,
                     deductible=1000.0, depreciation=0.0, confidence=0.90):
    return SettlementOutput(
        decision=decision, settlement_amount_usd=amount, gross_damage_usd=gross,
        deductible_applied_usd=deductible, depreciation_applied_usd=depreciation,
        calculation_breakdown=["ok"], confidence=confidence,
    )


def make_evaluation(passed=True, overall_score=0.95):
    return EvaluationOutput(
        accuracy_verified=True, accuracy_detail="ok",
        fairness=JudgeDimensionOutput(score=overall_score, reasoning="ok"),
        transparency=JudgeDimensionOutput(score=overall_score, reasoning="ok"),
        overall_score=overall_score, passed=passed, flags=[],
    )


def make_communication():
    return CommunicationOutput(
        subject="ok", message="ok", internal_notes="ok", next_steps=[], appeal_instructions=None,
    )


# ── The harness itself ──────────────────────────────────────────────────────

class ScenarioHarness:
    """Patches every agent-running function on src.agents.graph with a fake
    that returns the configured output (or a sensible default) and records
    how many times each was called. `consistency_sequence` lets a scenario
    return a DIFFERENT consistency_coordinator output on each successive
    call, to simulate a conflict resolving (or not) after a retry."""

    def __init__(
        self,
        intake=None, damage=None, policy=None, fraud=None,
        consistency=None, consistency_sequence=None,
        settlement=None, evaluation=None, guardrail_halt_after=None,
    ):
        self.intake = intake or make_intake()
        self.damage = damage or make_damage()
        self.policy = policy or make_policy()
        self.fraud = fraud or make_fraud()
        self.consistency_sequence = consistency_sequence or [consistency or make_consistency()]
        self.settlement = settlement or make_settlement()
        self.evaluation = evaluation if evaluation is not None else make_evaluation()
        # If set, every node call past this many TOTAL agent calls flips
        # guardrails_halted - simulates GuardrailsManager's budget halt
        # without needing to actually exhaust a real token/cost budget.
        self.guardrail_halt_after = guardrail_halt_after

        self.calls = {
            "intake": 0, "damage": 0, "policy": 0, "fraud": 0,
            "consistency": 0, "settlement": 0, "evaluator": 0, "communication": 0,
        }
        self._total_calls = 0

    def _halted(self) -> bool:
        self._total_calls += 1
        return self.guardrail_halt_after is not None and self._total_calls > self.guardrail_halt_after

    def _fake_intake(self, state):
        self.calls["intake"] += 1
        update = {
            "masked_claim": state["claim"],
            "intake_output": self.intake,
            "pipeline_trace": [{"agent": "intake_agent"}],
            "agent_call_count": 1,
        }
        if self._halted():
            update["guardrails_halted"] = True
        return update

    def _fake_damage(self, state):
        self.calls["damage"] += 1
        notes = self.damage.assessment_notes
        if state.get("consistency_retry_context"):
            notes = f"{notes} (retry_ctx_seen)"
        update = {
            "damage_output": self.damage.model_copy(update={"assessment_notes": notes}),
            "pipeline_trace": [{"agent": "damage_assessor"}], "agent_call_count": 1,
            "consistency_retry_context": None,
        }
        if self._halted():
            update["guardrails_halted"] = True
        return update

    def _fake_policy(self, state):
        self.calls["policy"] += 1
        notes = self.policy.coverage_notes
        if state.get("consistency_retry_context"):
            notes = f"{notes} (retry_ctx_seen)"
        update = {
            "policy_output": self.policy.model_copy(update={"coverage_notes": notes}),
            "pipeline_trace": [{"agent": "policy_checker"}], "agent_call_count": 1,
            "consistency_retry_context": None,
        }
        if self._halted():
            update["guardrails_halted"] = True
        return update

    def _fake_fraud(self, state):
        self.calls["fraud"] += 1
        update = {
            "fraud_output": self.fraud,
            "pipeline_trace": [{"agent": "fraud_crew"}], "agent_call_count": 3,
        }
        if self._halted():
            update["guardrails_halted"] = True
        return update

    def _fake_consistency(self, state):
        idx = min(self.calls["consistency"], len(self.consistency_sequence) - 1)
        output = self.consistency_sequence[idx]
        self.calls["consistency"] += 1
        update = {
            "consistency_output": output,
            "pipeline_trace": [{"agent": "consistency_coordinator", "call_n": self.calls["consistency"]}],
            "agent_call_count": 1,
        }
        if self._halted():
            update["guardrails_halted"] = True
        return update

    def _fake_settlement(self, state):
        self.calls["settlement"] += 1
        update = {
            "settlement_output": self.settlement,
            "final_decision": self.settlement.decision,
            "final_amount_usd": self.settlement.settlement_amount_usd,
            "pipeline_trace": [{"agent": "settlement_calculator"}], "agent_call_count": 1,
        }
        if self._halted():
            update["guardrails_halted"] = True
        return update

    def _fake_evaluator(self, state):
        self.calls["evaluator"] += 1
        if self.evaluation is None:
            return {
                "evaluation_passed": None, "evaluation_skipped": True,
                "pipeline_trace": [{"agent": "evaluator", "skipped": True}], "agent_call_count": 0,
            }
        update = {
            "evaluation_output": self.evaluation,
            "evaluation_passed": self.evaluation.passed, "evaluation_skipped": False,
            "pipeline_trace": [{"agent": "evaluator"}], "agent_call_count": 1,
        }
        if self._halted():
            update["guardrails_halted"] = True
        return update

    def _fake_communication(self, state):
        self.calls["communication"] += 1
        return {
            "communication_output": make_communication(),
            "pipeline_trace": [{"agent": "communication_agent"}],
        }

    @contextmanager
    def patched(self):
        """_build_graph_object() binds each node to its agent function BY
        VALUE at graph-build time (graph.add_node("intake_agent",
        guard_node("intake_agent", run_intake_agent)) captures the function
        object itself, not a later module-attribute lookup). get_compiled_graph()
        then caches that built graph in a module-level global for the whole
        process. So patch.object(g, "run_intake_agent", fake) only takes
        effect for a graph built AFTER the patch - any already-cached graph
        keeps calling the original, unpatched function forever. Confirmed by
        a real failure: the first scenario test in a pytest run passed (it
        built the graph fresh, under the patch), and every subsequent test
        in the same process silently ran against real, unmocked agents and
        a stale cached checkpointer from the first test. Forcing a fresh
        graph+checkpointer build under the patch, every time, is therefore
        required for test isolation - not an optimization to skip.

        Also a SEPARATE real bug found the hard way: _get_checkpointer()
        always resolves to the real, durable src/data/claims_checkpoints.db
        SQLite file (correct production behavior) whenever
        langgraph-checkpoint-sqlite is installed, which it is in this
        environment. Early versions of this harness called the real
        _get_checkpointer(), so scenario tests were writing to and resuming
        from that real file across SEPARATE pytest invocations -
        claim_ids like "CLM-SC-01" accumulated real checkpoint history
        across every past run of this suite and every scratch script run
        this session, so a "fresh" test resumed stale state instead of
        starting clean (confirmed: the file had grown to 6.5MB). Every
        scenario test therefore builds its own throwaway in-process
        MemorySaver directly - never the real checkpointer - so no
        scenario test can read or write the production checkpoint file."""
        if not getattr(self, "_graph_built", False):
            g._compiled_graph = None
            g._checkpointer = None
            g._checkpointer_ctx = None
        with patch.object(g, "run_intake_agent", self._fake_intake), \
             patch.object(g, "run_damage_assessor", self._fake_damage), \
             patch.object(g, "run_policy_checker", self._fake_policy), \
             patch.object(g, "run_fraud_crew", self._fake_fraud), \
             patch.object(g, "run_consistency_coordinator", self._fake_consistency), \
             patch.object(g, "run_settlement_calculator", self._fake_settlement), \
             patch.object(g, "run_evaluator", self._fake_evaluator), \
             patch.object(g, "run_communication_agent", self._fake_communication), \
             patch.object(g, "lookup_policy", lambda num: {"policy_number": num, "status": "active"}), \
             patch.object(g, "get_coverage_for_claim_type", lambda policy, t: {
                 "coverage_limit": 50000.0, "covered": True, "deductible": 1000.0, "exclusions": [],
             }):
            if not getattr(self, "_graph_built", False):
                g._compiled_graph = g._build_graph_object().compile(checkpointer=MemorySaver())
                self._graph_built = True
            yield self


def make_claim(claim_id: str, estimated_amount: float = 9000.0, **overrides) -> ClaimInput:
    base = dict(
        claim_id=claim_id, policy_number="POL-1", claimant_name="Test Claimant",
        claimant_email="t@t.com", claimant_phone="555-0100", claimant_dob="1990-01-01",
        incident_date="2026-01-01", incident_type="auto_collision",
        incident_description="Test incident description.", incident_location="",
        police_report_number=None, estimated_amount=estimated_amount,
        vehicle_year=2020, vehicle_make="Toyota", vehicle_model="Camry",
        documents=[], is_appeal=False, original_claim_id=None,
        type_disputed_by_claimant=False, type_dispute_reason=None,
    )
    base.update(overrides)
    return ClaimInput(**base)


def run_scenario(harness: ScenarioHarness, claim: ClaimInput) -> dict:
    """Runs the real compiled graph via process_claim, with every agent
    mocked per `harness`. Returns process_claim's own result dict:
    {"paused": bool, "state": {...}, "interrupt": {...}?}"""
    with harness.patched():
        return g.process_claim(claim)


def resume_scenario(harness: ScenarioHarness, claim_id: str, decision: dict) -> dict:
    """Resumes a paused scenario (same harness instance, so mocked agents
    stay consistent) via the real resume_claim/Command(resume=...) path."""
    with harness.patched():
        return g.resume_claim(claim_id, decision)


def trace_agents(state: dict) -> list[str]:
    return [e.get("agent") for e in (state.get("pipeline_trace") or [])]
