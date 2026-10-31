"""Claims routes: submit, list, get, process (background), status."""
from __future__ import annotations # for type hints that refer to classes defined later in the file

import json
import logging
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from api.db import Claim, User, engine, get_session
from api.security import get_current_user, require_role
from src.models.documents import CLAIMANT_FACING_MESSAGES, DocumentType
from src.security.upload_staging import (
    ALLOWED_SLOTS,
    claim_staged_documents,
    get_staged_manifest,
    record_staged_result,
    remove_staged_file,
    save_staged_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claims", tags=["Claims"])


class SubmitClaimRequest(BaseModel):
    policy_number: str
    incident_type: str
    incident_date: str
    incident_description: str
    incident_location: str = ""
    police_report_number: Optional[str] = None
    estimated_amount: float
    vehicle_year: Optional[int] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    documents: list[str] = []
    # Set only when the claimant kept their own incident_type after
    # /verify-type suggested a different one - see check_incident_type().
    type_disputed_by_claimant: bool = False
    type_dispute_reason: Optional[str] = None
    # Links this submission to files already uploaded via
    # /api/claims/upload-staging/{token}/{slot} before the claim existed.
    # None for a claim with no real document uploads (the only path that
    # existed before this field was added).
    upload_token: Optional[str] = None
    # Per-slot: the claimant explicitly chose "keep this file anyway" after
    # a non-blocking document_agent finding (wrong type, arithmetic/amount
    # mismatch) rather than re-uploading - same dispute-and-continue shape
    # as type_disputed_by_claimant/type_dispute_reason above, but per
    # document since more than one slot can be disputed independently.
    document_disputes: list[dict] = []
    # Only set by the Evals tab's "Run this case" golden-set flow
    # (api/routes_evals.py's /cases/{id}/materialize) - already-structured
    # AutoCollisionDocuments content (not PDF files) for a case that has no
    # real document to stage. Deliberate, narrow exception to the upload
    # path - see routes_evals.py's materialize_case() docstring for why.
    prebuilt_documents: Optional[dict] = None


class VerifyTypeRequest(BaseModel):
    incident_type: str
    incident_description: str


def _claim_to_dict(c: Claim) -> dict:
    return {
        "id": c.id,
        "claim_id": c.claim_id,
        "user_id": c.user_id,
        "policy_number": c.policy_number,
        "incident_type": c.incident_type,
        "incident_date": c.incident_date,
        "incident_description": c.incident_description,
        "incident_location": c.incident_location,
        "estimated_amount": c.estimated_amount,
        "settlement_amount": c.settlement_amount,
        "status": c.status,
        "final_decision": c.final_decision,
        "fraud_score": c.fraud_score,
        "fraud_risk_level": c.fraud_risk_level,
        "evaluation_score": c.evaluation_score,
        "decided_by": c.decided_by,
        "decided_at": c.decided_at,
        "hitl_required": c.hitl_required,
        "hitl_ticket_id": c.hitl_ticket_id,
        "agent_call_count": c.agent_call_count,
        "total_cost_usd": c.total_cost_usd,
        "processing_time_sec": c.processing_time_sec,
        "denial_reasons": json.loads(c.denial_reasons or "[]"),
        "communication_message": c.communication_message,
        "appeal_instructions": c.appeal_instructions,
        "agent_outputs": json.loads(c.agent_outputs or "{}"),
        "pipeline_path": json.loads(c.pipeline_path or "[]"),
        "error_log": json.loads(c.error_log or "[]"),
        "type_disputed_by_claimant": c.type_disputed_by_claimant,
        "type_dispute_reason": c.type_dispute_reason,
        "created_at": c.created_at,
        "completed_at": c.completed_at,
    }


def _persist_progress(claim_pk: int, state: dict) -> None:
    """on_step callback for process_claim/resume_claim's streaming mode -
    writes just agent_outputs/pipeline_path (the two fields the
    Observability view's step circles and agent-detail panel read) after
    each super-step, in its own short-lived session/commit so a reviewer
    polling GET /api/claims/{id} mid-run sees live progress instead of
    everything jumping to 'done' at once when the whole pipeline finishes.
    Deliberately NOT the full _persist_pipeline_result/_persist_pause logic
    (status, settlement_amount, decided_by, etc.) - those only make sense
    once the run is actually terminal or paused."""
    try:
        with Session(engine) as session:
            claim = session.get(Claim, claim_pk)
            if not claim:
                return
            trace = state.get("pipeline_trace") or []
            path = [entry.get("agent") for entry in trace if isinstance(entry, dict) and entry.get("agent")]
            claim.pipeline_path = json.dumps(path)

            agent_outputs = {}
            for key in ("intake_output", "fraud_output", "damage_output", "policy_output",
                        "settlement_output", "evaluation_output", "communication_output"):
                obj = state.get(key)
                if obj and hasattr(obj, "model_dump"):
                    try:
                        agent_outputs[key] = obj.model_dump(mode="json")
                    except Exception:
                        agent_outputs[key] = str(obj)
            agent_outputs["_trace"] = trace
            claim.agent_outputs = json.dumps(agent_outputs, default=str)

            session.add(claim)
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[claim_pk=%s] could not persist live progress (non-fatal)", claim_pk)


def _persist_pipeline_result(claim: Claim, state: dict, elapsed: float) -> None:
    """Copy results from a terminal (non-paused) ClaimsState onto the Claim row."""

    from datetime import datetime, timezone

    decision = state.get("final_decision")
    claim.final_decision = decision.value if hasattr(decision, "value") else (str(decision) if decision else None)
    claim.settlement_amount = state.get("final_amount_usd")

    # Track who made the final decision and when
    claim.decided_at = datetime.now(timezone.utc).isoformat()
    if state.get("hitl_required") and state.get("human_reviewer_id"):
        claim.decided_by = state["human_reviewer_id"]
    else:
        claim.decided_by = "AI Agent"

    fraud = state.get("fraud_output")
    if fraud:
        claim.fraud_score = float(getattr(fraud, "fraud_score", 0.0))
        level = getattr(fraud, "fraud_risk_level", None)
        claim.fraud_risk_level = level.value if hasattr(level, "value") else (str(level) if level else None)

    evaluation = state.get("evaluation_output")
    if evaluation:
        claim.evaluation_score = float(getattr(evaluation, "overall_score", 0.0))

    claim.hitl_required = bool(state.get("hitl_required"))
    claim.hitl_ticket_id = state.get("hitl_ticket_id") or claim.hitl_ticket_id
    claim.agent_call_count = int(state.get("agent_call_count") or 0)
    claim.total_tokens_used = int(state.get("total_tokens_used") or 0)
    claim.total_cost_usd = float(state.get("total_cost_usd") or 0.0)
    claim.processing_time_sec = (claim.processing_time_sec or 0.0) + elapsed

    trace = state.get("pipeline_trace") or []
    path = [entry.get("agent") for entry in trace if isinstance(entry, dict) and entry.get("agent")]
    claim.pipeline_path = json.dumps(path)
    claim.error_log = json.dumps(state.get("error_log") or [])

    # Serialize enriched per-agent outputs for transparency
    agent_outputs = {}
    for key in ("intake_output", "fraud_output", "damage_output", "policy_output",
                "settlement_output", "evaluation_output", "communication_output"):
        obj = state.get(key)
        if obj and hasattr(obj, "model_dump"):
            try:
                dumped = obj.model_dump(mode="json")
                agent_outputs[key] = dumped
            except Exception:
                agent_outputs[key] = str(obj)
    agent_outputs["_trace"] = trace  # enriched pipeline_trace with reasoning/flags
    claim.agent_outputs = json.dumps(agent_outputs, default=str)

    # Map final_decision to a user-facing status instead of always "completed"
    decision_str = claim.final_decision or ""
    _DECISION_TO_STATUS = {
        "approved": "approved",
        "approved_partial": "approved_partial",
        "denied": "denied",
        "auto_rejected": "auto_rejected",
        "fraud_investigation": "fraud_investigation",
        "pending_documents": "pending_documents",
    }
    claim.status = _DECISION_TO_STATUS.get(decision_str, "completed")

    # Persist denial reasons and communication output
    settlement = state.get("settlement_output")
    if settlement:
        claim.denial_reasons = json.dumps(getattr(settlement, "denial_reasons", None) or [])
    elif claim.final_decision == "denied":
        # No settlement output (e.g. intake rejection) - pull reasons from intake
        intake = state.get("intake_output")
        if intake:
            reasons = list(getattr(intake, "validation_flags", None) or [])
            if not reasons and getattr(intake, "intake_notes", ""):
                reasons = [intake.intake_notes]
            claim.denial_reasons = json.dumps(reasons)
    comm = state.get("communication_output")
    if comm:
        claim.communication_message = getattr(comm, "message", None)
        claim.appeal_instructions = getattr(comm, "appeal_instructions", None)

    claim.completed_at = datetime.now(timezone.utc).isoformat()


def _persist_pause(claim: Claim, interrupt_payload: dict, state: Optional[dict] = None,
                   elapsed: float = 0.0) -> None:
    """Pipeline paused at HITL - mark the claim so approvers can act on it.

    Used for the first pause AND for a second pause after resume (e.g. the
    fraud review was approved, then the evaluator's quality gate failed). The
    ticket id must come from THIS pause, or the claim keeps pointing at the
    ticket that was just resolved and nobody can act on the new one.
    """
    claim.status = "pending_human_review"
    claim.hitl_required = True
    claim.hitl_ticket_id = interrupt_payload.get("ticket_id") or claim.hitl_ticket_id
    if state is None:
        return
    claim.agent_call_count = int(state.get("agent_call_count") or 0)
    claim.total_tokens_used = int(state.get("total_tokens_used") or 0)
    claim.total_cost_usd = float(state.get("total_cost_usd") or 0.0)
    claim.processing_time_sec = (claim.processing_time_sec or 0.0) + elapsed
    fraud = state.get("fraud_output")
    if fraud:
        claim.fraud_score = float(getattr(fraud, "fraud_score", 0.0))
        level = getattr(fraud, "fraud_risk_level", None)
        claim.fraud_risk_level = level.value if hasattr(level, "value") else None
    # Persist agent outputs so HITL reviewers can see agent traces
    agent_outputs = {}
    for key in ("intake_output", "fraud_output", "damage_output", "policy_output",
                "settlement_output", "evaluation_output"):
        obj = state.get(key)
        if obj and hasattr(obj, "model_dump"):
            try:
                agent_outputs[key] = obj.model_dump(mode="json")
            except Exception:
                agent_outputs[key] = str(obj)
    trace = state.get("pipeline_trace") or []
    agent_outputs["_trace"] = trace
    claim.agent_outputs = json.dumps(agent_outputs, default=str)
    path = [entry.get("agent") for entry in trace if isinstance(entry, dict) and entry.get("agent")]
    claim.pipeline_path = json.dumps(path)


def _spawn_pipeline(claim_pk: int) -> None:
    """Run _run_pipeline in a real daemon thread.

    FastAPI's BackgroundTasks delegates sync tasks to anyio's threadpool, which
    has been unreliable here for long-running LLM work (the task silently
    vanishes and the claim sits at 'processing' forever). A plain threading
    Thread with daemon=True bypasses that entirely - the OS just runs it.
    """
    
    t = threading.Thread(target=_run_pipeline, args=(claim_pk,), daemon=True,
                         name=f"pipeline-{claim_pk}")
    t.start()
    logger.info("[claim_pk=%s] pipeline thread started (tid=%s)", claim_pk, t.ident)


def _mark_failed(claim_pk: int, error: str) -> None:
    """Last-resort status flip used when _run_pipeline crashes outside its normal try/except."""
    
    from datetime import datetime, timezone
    try:
        with Session(engine) as session:
            claim = session.get(Claim, claim_pk)
            if not claim:
                return
            claim.status = "failed"
            claim.error_log = json.dumps([error[:500]])
            claim.completed_at = datetime.now(timezone.utc).isoformat()
            session.add(claim)
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[claim_pk=%s] could not even mark as failed", claim_pk)


def _run_pipeline(claim_pk: int) -> None:
    """Background task: run the LangGraph pipeline until completion OR HITL pause.

    Wrapped in a bare try/except so that ANY failure (import error, LLM key
    missing, SQLite lock, etc.) is surfaced to the backend log AND persisted
    on the claim row - otherwise the row hangs at 'processing' forever and the
    reviewer has no way to know why.
    """
    
    import traceback
    logger.info("[claim_pk=%s] _run_pipeline entered", claim_pk)

    try:
        from datetime import datetime, timezone
        from src.agents.graph import process_claim
        from src.models.state import ClaimInput
    except Exception as e:  # noqa: BLE001
        logger.exception("[claim_pk=%s] pipeline imports failed", claim_pk)
        _mark_failed(claim_pk, f"import_failed: {e}\n{traceback.format_exc()}")
        return

    try:
        with Session(engine) as session:
            claim = session.get(Claim, claim_pk)
            if not claim:
                logger.error("[claim_pk=%s] claim row vanished", claim_pk)
                return

            logger.info("[%s] pipeline starting (provider-driven agents)", claim.claim_id)
            claim.status = "processing"
            session.add(claim)
            session.commit()
            session.refresh(claim)

            pipeline_input = ClaimInput(
                claim_id=claim.claim_id,
                policy_number=claim.policy_number,
                claimant_name=claim.claimant_name or "Unknown",
                claimant_email=claim.claimant_email or "unknown@example.com",
                claimant_phone=claim.claimant_phone or "000-000-0000",
                claimant_dob=claim.claimant_dob or "1990-01-01",
                incident_date=claim.incident_date,
                incident_type=claim.incident_type,
                incident_description=claim.incident_description,
                incident_location=claim.incident_location,
                police_report_number=claim.police_report_number,
                estimated_amount=claim.estimated_amount,
                vehicle_year=claim.vehicle_year,
                vehicle_make=claim.vehicle_make,
                vehicle_model=claim.vehicle_model,
                documents=json.loads(claim.documents or "[]"),
                is_appeal=False,
                original_claim_id=None,
                type_disputed_by_claimant=claim.type_disputed_by_claimant,
                type_dispute_reason=claim.type_dispute_reason,
            )

            uploaded_documents = None
            if claim.upload_token:
                from src.security.upload_staging import claim_staged_documents
                uploaded_documents = claim_staged_documents(claim.upload_token, claim.claim_id) or None

            # Golden-set "Run this case" flow only - already-structured
            # document content with no PDF file to stage (see
            # SubmitClaimRequest.prebuilt_documents' own comment).
            prebuilt_documents = json.loads(claim.prebuilt_documents) if claim.prebuilt_documents else None

            t0 = time.time()
            try:
                result = process_claim(
                    pipeline_input, documents=prebuilt_documents, uploaded_documents=uploaded_documents,
                    on_step=lambda s: _persist_progress(claim_pk, s),
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("[%s] process_claim raised", claim.claim_id)
                claim.status = "failed"
                claim.error_log = json.dumps([f"{type(e).__name__}: {e}"])
                claim.completed_at = datetime.now(timezone.utc).isoformat()
                session.add(claim)
                session.commit()
                return

            elapsed = time.time() - t0
            state = result["state"]

            if result.get("paused"):
                _persist_pause(claim, result.get("interrupt") or {}, state, elapsed)
            else:
                _persist_pipeline_result(claim, state, elapsed)

            session.add(claim)
            session.commit()
            logger.info(
                "[%s] pipeline %s in %.2fs",
                claim.claim_id,
                "paused" if result.get("paused") else "completed",
                elapsed,
            )
    except Exception as e:  # noqa: BLE001
        # Absolutely last resort - never let this task die silently.
        logger.exception("[claim_pk=%s] unhandled error in _run_pipeline", claim_pk)
        _mark_failed(claim_pk, f"unhandled: {type(e).__name__}: {e}")


def resume_pipeline_for_claim(claim_id: str, decision: dict) -> dict:
    """
    Called from the HITL decide endpoint after an approver submits a decision.
    Marks claim as 'processing' immediately, then resumes the pipeline in a
    background thread (same pattern as initial submission).
    """
    
    with Session(engine) as session:
        claim = session.exec(select(Claim).where(Claim.claim_id == claim_id)).first()
        if not claim:
            raise HTTPException(status_code=404, detail=f"Claim {claim_id} not found")
        if claim.status != "pending_human_review":
            raise HTTPException(
                status_code=400,
                detail=f"Claim {claim_id} is not awaiting review (status={claim.status})",
            )
        claim.status = "processing"
        session.add(claim)
        session.commit()

        # Run the remaining pipeline in a background thread
        t = threading.Thread(
            target=_run_resume, args=(claim.id, claim_id, decision),
            daemon=True, name=f"resume-{claim_id}",
        )
        t.start()
        logger.info("[%s] resume thread started (tid=%s)", claim_id, t.ident)

        return _claim_to_dict(claim)


def _run_resume(claim_pk: int, claim_id: str, decision: dict) -> None:
    """Background thread: resume the paused LangGraph pipeline after HITL approval."""
    
    import traceback
    logger.info("[%s] _run_resume entered", claim_id)

    try:
        from src.agents.graph import resume_claim
    except Exception as e:
        logger.exception("[%s] resume imports failed", claim_id)
        _mark_failed(claim_pk, f"import_failed: {e}\n{traceback.format_exc()}")
        return

    try:
        with Session(engine) as session:
            claim = session.get(Claim, claim_pk)
            if not claim:
                logger.error("[%s] claim row vanished during resume", claim_id)
                return

            t0 = time.time()
            try:
                result = resume_claim(claim_id, decision, on_step=lambda s: _persist_progress(claim_pk, s))
            except Exception as e:
                logger.exception("[%s] resume_claim raised", claim_id)
                claim.status = "failed"
                claim.error_log = json.dumps([f"resume_failed: {e}"])
                from datetime import datetime, timezone
                claim.completed_at = datetime.now(timezone.utc).isoformat()
                session.add(claim)
                session.commit()
                return

            elapsed = time.time() - t0
            state = result["state"]

            if result.get("paused"):
                # Paused again on the same thread with a NEW ticket.
                _persist_pause(claim, result.get("interrupt") or {}, state, elapsed)
            else:
                _persist_pipeline_result(claim, state, elapsed)

            session.add(claim)
            session.commit()
            logger.info("[%s] resume completed in %.2fs", claim_id, elapsed)
    except Exception as e:
        logger.exception("[%s] unhandled error in _run_resume", claim_id)
        _mark_failed(claim_pk, f"unhandled_resume: {type(e).__name__}: {e}")


@router.post("/verify-type")
def verify_incident_type(
    body: VerifyTypeRequest,
    _: User = Depends(get_current_user),
):
    """Classify incident_type from the description ALONE (the claimant's own
    selection is never shown to the model) and compare against what the
    claimant picked. Runs synchronously, before the claim is created, so a
    mismatch can be resolved in the form itself rather than on an
    already-submitted claim. Returns match=True with no suggestion when
    they agree, or when the description is too short/ambiguous to classify
    at all (see check_incident_type's None return) - in both cases there's
    nothing to flag."""
    from src.security.incident_type_check import check_incident_type

    result = check_incident_type(body.incident_description, body.incident_type)
    if result is None or result.classified_type.value == body.incident_type:
        return {"match": True}

    return {
        "match": False,
        "suggested_type": result.classified_type.value,
        "reasoning": result.reasoning,
        "confidence": result.confidence,
    }


_SLOT_TO_DOCUMENT_TYPE = {
    "police_report": DocumentType.POLICE_REPORT,
    "other_driver_info": DocumentType.OTHER_DRIVER_INFO,
    "repair_estimate": DocumentType.REPAIR_ESTIMATE,
}


@router.post("/upload-staging/{token}/{slot}")
def upload_staged_document(
    token: str,
    slot: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Upload one document into a pre-claim staging slot and run the same
    per-slot extraction document_agent runs, for fast claimant-facing
    feedback before the claim even exists. A later upload to the same
    token+slot overwrites the previous one (re-upload flow). The real
    pipeline run (src/agents/document_agent.py, inside process_claim) runs
    its OWN extraction again on submit - this is a preview, not a cache."""
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(status_code=400, detail=f"Invalid slot: {slot}")

    content = file.file.read()
    try:
        path = save_staged_file(token, slot, file.filename or "upload.pdf", content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # "other" files are stored but never run through structured extraction -
    # same as the original document-agent design (AutoCollisionDocuments only
    # covers the 3 required slots).
    if slot == "other":
        record_staged_result(token, slot, path, status="ok", message="Uploaded.", finding=None)
        return {"status": "ok", "message": "Uploaded."}

    from src.agents.document_agent import _process_slot
    from src.models.documents import DocumentVerificationFinding

    doc_type = _SLOT_TO_DOCUMENT_TYPE[slot]
    findings: list[DocumentVerificationFinding] = []
    _extracted, _calls = _process_slot(doc_type, str(path), findings)

    if not findings:
        status, message, finding_dict = "ok", "Uploaded and verified.", None
    else:
        finding = findings[0]
        finding_dict = finding.model_dump(mode="json")
        status = "failed" if finding.issue == "could_not_parse" else "problem"
        message = CLAIMANT_FACING_MESSAGES.get(finding.issue, "This document needs a closer look.")

    record_staged_result(token, slot, path, status=status, message=message, finding=finding_dict)
    return {"status": status, "message": message, "finding": finding_dict}


@router.delete("/upload-staging/{token}/{slot}")
def delete_staged_document(token: str, slot: str, user: User = Depends(get_current_user)):
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(status_code=400, detail=f"Invalid slot: {slot}")
    try:
        remove_staged_file(token, slot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "removed"}


@router.get("/upload-staging/{token}")
def get_staged_documents(token: str, user: User = Depends(get_current_user)):
    return {"documents": get_staged_manifest(token)}


# ── Claimant-facing demo scenarios ───────────────────────────────────────
# A curated subset of the same golden-set / test_pdfs cases the admin-only
# Evals tab's "Run this case" button already uses (api/routes_evals.py) -
# reuses that endpoint's exact materialize logic, just exposed under plain
# claimant auth (get_current_user, not require_role) and narrowed to 5
# cases chosen to look good in a live demo (clean approve, total loss,
# high-value pause, injection pause, real PDF upload) rather than every
# developer-facing edge case (corrupt file, OCR fallback, etc.) which
# would look like a bug to a non-technical audience.
_DEMO_SCENARIO_IDS = ["GOLD-01", "GOLD-04", "GOLD-15", "GOLD-16", "PDF-01"]


@router.get("/demo-scenarios")
def list_demo_scenarios():
    from src.data.golden import load_specs

    from api.routes_evals import _load_test_pdfs_manifest

    specs_by_id = {s["id"]: s for s in load_specs()}
    manifest = _load_test_pdfs_manifest()
    pdf_by_id = {c["id"]: c for c in manifest.get("cases", [])}

    scenarios = []
    for case_id in _DEMO_SCENARIO_IDS:
        if case_id in specs_by_id:
            spec = specs_by_id[case_id]
            scenarios.append({
                "id": case_id,
                "label": spec.get("slug", case_id).replace("_", " ").title(),
                "description": spec.get("rule_citation", ""),
            })
        elif case_id in pdf_by_id:
            case = pdf_by_id[case_id]
            scenarios.append({"id": case_id, "label": case["label"], "description": case.get("description", "")})
    return {"scenarios": scenarios}


@router.get("/demo-scenarios/{case_id}/materialize")
def materialize_demo_scenario(case_id: str):
    """Same materialize contract as api/routes_evals.py's
    /cases/{id}/materialize - reusing that function directly rather than
    duplicating it, since this is only a narrower, claimant-facing door
    onto the same logic."""
    if case_id not in _DEMO_SCENARIO_IDS:
        raise HTTPException(status_code=404, detail=f"Unknown demo scenario: {case_id}")

    from api.routes_evals import materialize_case

    return materialize_case(case_id)


@router.get("/demo-scenarios/{case_id}/pdf/{slot}")
def get_demo_scenario_pdf(case_id: str, slot: str):
    """Claimant-reachable mirror of routes_evals.get_case_pdf, for the
    PDF-01 demo scenario's file staging."""
    if case_id not in _DEMO_SCENARIO_IDS:
        raise HTTPException(status_code=404, detail=f"Unknown demo scenario: {case_id}")

    from api.routes_evals import get_case_pdf

    return get_case_pdf(case_id, slot)


@router.post("/submit")
def submit_claim(
    body: SubmitClaimRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Submit a new claim - creates the Claim row, then launches the pipeline in a background thread."""

    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    claim = Claim(
        claim_id=claim_id,
        user_id=user.id,
        policy_number=body.policy_number,
        claimant_name=user.username,
        claimant_email=user.email,
        incident_date=body.incident_date,
        incident_type=body.incident_type,
        incident_description=body.incident_description,
        incident_location=body.incident_location,
        police_report_number=body.police_report_number,
        estimated_amount=body.estimated_amount,
        vehicle_year=body.vehicle_year,
        vehicle_make=body.vehicle_make,
        vehicle_model=body.vehicle_model,
        documents=json.dumps(body.documents),
        type_disputed_by_claimant=body.type_disputed_by_claimant,
        type_dispute_reason=body.type_dispute_reason,
        document_disputes=json.dumps(body.document_disputes),
        upload_token=body.upload_token,
        prebuilt_documents=json.dumps(body.prebuilt_documents) if body.prebuilt_documents is not None else None,
        status="submitted",
    )
    session.add(claim)
    session.commit()
    session.refresh(claim)
    logger.info("[%s] submitted by user_id=%s, launching pipeline thread", claim.claim_id, user.id)
    _spawn_pipeline(claim.id)
    return _claim_to_dict(claim)


@router.get("/all")
def get_all_claims(
    status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(get_session),
    user: User = Depends(require_role("reviewer", "admin")),
):
    """Every claim in the system: reviewers and admins only. Claimants use
    /api/claims/user/{their id}."""
    stmt = select(Claim)
    if status:
        stmt = stmt.where(Claim.status == status)
    stmt = stmt.order_by(Claim.created_at.desc()).limit(limit)
    rows = session.exec(stmt).all()
    return {"claims": [_claim_to_dict(c) for c in rows]}


@router.get("/user/{user_id}")
def get_user_claims(
    user_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if user.role == "user" and user.id != user_id:
        raise HTTPException(status_code=403, detail="Cannot view other users' claims")
    rows = session.exec(
        select(Claim).where(Claim.user_id == user_id).order_by(Claim.created_at.desc())
    ).all()
    return {"claims": [_claim_to_dict(c) for c in rows]}


@router.get("/{claim_id}")
def get_claim(
    claim_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    claim = session.exec(select(Claim).where(Claim.claim_id == claim_id)).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    if user.role == "user" and claim.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your claim")
    return _claim_to_dict(claim)


@router.post("/{claim_id}/process")
def reprocess_claim(
    claim_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_role("reviewer", "admin")),
):
    claim = session.exec(select(Claim).where(Claim.claim_id == claim_id)).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    claim.status = "processing"
    session.add(claim)
    session.commit()
    logger.info("[%s] reprocess requested, launching pipeline thread (claim_pk=%s)", claim.claim_id, claim.id)
    _spawn_pipeline(claim.id)
    return {"ok": True, "claim_id": claim_id, "status": "processing"}


@router.get("/{claim_id}/status")
def get_claim_status(
    claim_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    claim = session.exec(select(Claim).where(Claim.claim_id == claim_id)).first()
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    if user.role == "user" and claim.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your claim")
    return {
        "claim_id": claim.claim_id,
        "status": claim.status,
        "final_decision": claim.final_decision,
        "hitl_required": claim.hitl_required,
        "hitl_ticket_id": claim.hitl_ticket_id,
    }
