from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.agent.models import AgentRun
from app.agent.queue import ACTIVE_RUNS, utcnow
from app.agent.schemas import MAX_MESSAGE, RunStatus, clean_text
from app.auth import SESSION_COOKIE, CurrentUser, WriteUser, csrf_value
from app.companion.actions import action_scope, execution_command, log_action, proposal_view
from app.companion.schemas import ActionDecision, ActionFeedback, ActionInvalidation, ChatInput, Hint, PageContext
from app.companion.service import CHAT_TTL, PAGE_LABELS, clear_transcript, require_trainee, scoped_context, scoped_result
from app.companion.workflows import (
    ACTIVE as WORKFLOW_ACTIVE, WorkflowControl, active_workflow, after_feedback, effective_state,
    initial_workflow, pause_or_cancel, queue_resume, requested_workflow, select_sop, workflow_view,
)
from app.db import Database
from app.http import ApiError

router = APIRouter(prefix="/companion/chat", tags=["companion chat"])


def lock_chat(db, user_id):
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(user_id), 23173))))


def conversation_rows(user_id, conversation_id):
    return select(AgentRun).where(
        AgentRun.requested_by == user_id, AgentRun.created_at >= utcnow() - CHAT_TTL,
        AgentRun.companion["conversation_id"].astext == str(conversation_id),
    )


def session_binding(request: Request) -> str:
    return csrf_value("companion-action:" + request.cookies[SESSION_COOKIE])


def valid_hint(body: ChatInput, facts: list[dict]) -> Hint | None:
    context = body.page_context
    hint = body.active_hint_id
    if hint == "SOP_SEARCH_EMPTY":
        return hint if context.search_state == "empty" and context.page_id in {"ticket", "workspace"} else None
    if hint == "ANALYSIS_COMPLETED":
        return hint if any(fact.get("prior_analysis_status") == RunStatus.SUCCEEDED for fact in facts) else None
    if hint in {"TICKET_SELECTED", "TICKET_STATUS_CHANGED", "WORK_EDIT_STARTED"}:
        return hint if context.page_id == "ticket" else None
    if hint == "HANDOVER_OPENED":
        return hint if context.page_id == "handover" or (context.page_id == "workspace" and context.section == "outgoing") else None
    if context.page_id == "workspace":
        if hint == "TICKET_LIST_READY" and context.section in {None, "tickets"}:
            return hint
        if hint == "INCOMING_OPENED" and context.section == "schedule":
            return hint
        if hint == "HANDOVER_EDIT_STARTED":
            return hint
    return None


def turn_view(db, run: AgentRun):
    context = PageContext.model_validate(run.companion["page_context"])
    result = run.result or {}
    workflow = workflow_view(run, result, utcnow())
    status = run.status
    error = {"code": run.error_code, "message": run.error_message, "retryable": run.retryable} if run.error_code else None
    try:
        info = scoped_result(db, run.requested_by, context, result)
        info = result.get("context_info", info)
    except ApiError:
        info = {"page_id": context.page_id, "label": PAGE_LABELS[context.page_id]}
        status, result = RunStatus.FAILED, {}
        error = {"code": "CHAT_CONTEXT_UNAVAILABLE", "message": "이 질문의 업무 데이터 또는 출처에 접근할 수 없습니다.", "retryable": False}
        if workflow:
            workflow = {**workflow, "status": "FAILED", "error_code": "CHAT_CONTEXT_UNAVAILABLE", "can_resume": False}
    mode = None
    if status == RunStatus.FAILED:
        mode = "error"
    elif status == RunStatus.SUCCEEDED:
        mode = {"fake": "mock", "gemini": "gemini", "workflow": "workflow"}.get(run.provider)
    return {"conversation_id": run.companion["conversation_id"], "message_id": run.id,
            "request_id": run.companion["request_id"], "message": run.message,
            "active_hint_id": run.companion.get("active_hint_id"), "status": status,
            "context_info": info, "timestamp": run.created_at,
            "answer": result.get("answer"), "sources": result.get("sources", []),
            "retrieval": result.get("retrieval"), "intent": result.get("intent"), "error": error,
            "search_mode": result.get("search_mode"),
            "provider": run.provider, "model": run.model, "execution_mode": mode,
            "tutor_level": (run.companion.get("tutor_hint") or {}).get("level"),
            "action_proposal": proposal_view(run, result, utcnow()), "workflow": workflow,
            "sop_comparison": (result.get("workflow") or {}).get("data", {}).get("sop_comparison")}


@router.post("", status_code=202)
def chat(body: ChatInput, db: Database, user: WriteUser, request: Request, response: Response) -> dict:
    require_trainee(user)
    response.headers["Cache-Control"] = "no-store"
    context = body.page_context
    _, facts, _ = scoped_context(db, user.id, context)
    lock_chat(db, user.id)
    message = clean_text(body.message, MAX_MESSAGE)
    metadata = {"conversation_id": str(body.conversation_id), "request_id": str(body.request_id),
                "page_context": context.model_dump(mode="json"), "active_hint_id": valid_hint(body, facts)}
    if body.action_view_id:
        metadata.update(action_view_id=str(body.action_view_id), action_session=session_binding(request))
        if kind := requested_workflow(message, context):
            metadata["workflow_kind"] = kind
    if body.tutor_offer_id:
        from app.companion.tutor_routes import linked_workflow

        metadata.update(tutor_offer_id=str(body.tutor_offer_id), tutor_tab_id=str(body.tutor_tab_id))
        if body.action_view_id and (kind := linked_workflow(message)):
            metadata["workflow_kind"] = kind
    previous = db.scalar(conversation_rows(user.id, body.conversation_id).where(
        AgentRun.companion["request_id"].astext == str(body.request_id)))
    if previous:
        tutor = previous.companion.get("tutor_hint") or {}
        if (body.tutor_offer_id and tutor.get("id") == str(body.tutor_offer_id)
                and tutor.get("tab_id") == str(body.tutor_tab_id) and previous.message == message
                and previous.companion["page_context"] == metadata["page_context"]):
            return turn_view(db, previous)
        prior = {key: value for key, value in previous.companion.items() if key not in {"action_view_closed", "linked_tutor_hint"}}
        if previous.message != message or prior != metadata:
            raise ApiError(409, "CHAT_REQUEST_CONFLICT", "같은 요청 ID에 다른 질문을 보낼 수 없습니다.")
        return turn_view(db, previous)
    now = utcnow()
    kind = metadata.get("workflow_kind")
    if kind and active_workflow(db, user.id, now):
        raise ApiError(409, "WORKFLOW_BUSY", "기존 워크플로를 마치거나 취소한 뒤 새 작업을 시작해 주세요.")
    count = db.scalar(select(func.count()).select_from(AgentRun).where(
        AgentRun.requested_by == user.id, AgentRun.companion.is_not(None), AgentRun.created_at >= now - timedelta(minutes=1)))
    if count >= 8:
        raise ApiError(429, "CHAT_RATE_LIMIT", "키로 질문이 많습니다. 잠시 뒤 다시 시도하세요.")
    if body.tutor_offer_id:
        from app.companion.tutor_routes import TUTOR_MORE, linked_offer, log_event, queue_hint

        offer, last_hint = linked_offer(db, user.id, request, body, now)
        if kind:
            log_event(offer, "WORKFLOW_REQUESTED", now, kind=kind)
        elif TUTOR_MORE.search(message):
            if active_workflow(db, user.id, now):
                raise ApiError(409, "WORKFLOW_BUSY", "진행 중인 워크플로가 끝난 뒤 힌트를 요청해 주세요.")
            hint_run = queue_hint(db, offer, context, body.request_id, "more", now, conversation_id=body.conversation_id, message=message)
            return turn_view(db, hint_run)
        metadata["linked_tutor_hint"] = str(last_hint.id)
    result = {"workflow": initial_workflow(kind, context, str(body.action_view_id), now)} if kind else None
    run = AgentRun(requested_by=user.id, ticket_id=context.ticket_id, message=message, simulation_run_id=context.run_id,
                   status=RunStatus.QUEUED, created_at=now, companion=metadata, result=result)
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "uq_agent_runs_active_user":
            raise
        raise ApiError(409, "AGENT_BUSY", "이전 질문이나 분석이 처리 중입니다. 잠시 뒤 다시 시도하세요.") from exc
    return turn_view(db, run)


@router.get("/{conversation_id}")
def chat_history(conversation_id: UUID, db: Database, user: CurrentUser, response: Response) -> dict:
    require_trainee(user)
    response.headers["Cache-Control"] = "no-store"
    turns = db.scalars(conversation_rows(user.id, conversation_id).order_by(
        AgentRun.created_at.desc(), AgentRun.id.desc()).limit(40)).all()
    return {"conversation_id": conversation_id, "turns": [turn_view(db, turn) for turn in reversed(turns)]}


def locked_proposal(db, user, request, conversation_id, message_id, body):
    require_trainee(user)
    run = db.scalar(conversation_rows(user.id, conversation_id).where(AgentRun.id == message_id).with_for_update())
    proposal = (run.result or {}).get("action_proposal") if run else None
    if (not proposal or proposal["id"] != str(body.proposal_id) or proposal["view_id"] != str(body.view_id)
            or run.companion.get("action_session") != session_binding(request)):
        raise ApiError(404, "ACTION_NOT_FOUND", "실행 제안을 찾을 수 없습니다.")
    return run, proposal


@router.post("/{conversation_id}/actions/invalidate", status_code=204)
def invalidate_view(conversation_id: UUID, body: ActionInvalidation, db: Database, user: WriteUser, request: Request) -> None:
    require_trainee(user)
    lock_chat(db, user.id)
    rows = db.scalars(conversation_rows(user.id, conversation_id).where(
        or_(AgentRun.companion["action_view_id"].astext == str(body.view_id),
            AgentRun.result["workflow"]["view_id"].astext == str(body.view_id)),
        AgentRun.companion["action_session"].astext == session_binding(request),
    ).with_for_update())
    for run in rows:
        result = run.result or {}
        flow = result.get("workflow")
        if flow:
            proposal = result.get("action_proposal") or {}
            approved_transition = (proposal.get("status") == "approved" and proposal.get("type") in {"navigate_to_page", "open_sop"}
                                   and proposal.get("id") == str(body.transition_proposal_id))
            if (flow["view_id"] == str(body.view_id) and flow["status"] in WORKFLOW_ACTIVE
                    and flow["status"] != "PAUSED" and not approved_transition):
                pause_or_cancel(run, "PAUSED", utcnow(), reason="WORKFLOW_CONTEXT_CHANGED")
            continue
        if run.companion.get("action_view_closed"):
            continue
        run.companion = {**run.companion, "action_view_closed": True}
        if proposal := (run.result or {}).get("action_proposal"):
            if proposal["status"] == "pending":
                log_action(run, proposal, "stale")
    db.commit()


@router.post("/{conversation_id}/actions/{message_id}")
def decide_action(conversation_id: UUID, message_id: UUID, body: ActionDecision,
                  db: Database, user: WriteUser, request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    run, proposal = locked_proposal(db, user, request, conversation_id, message_id, body)
    now = utcnow()
    current = proposal_view(run, run.result, now)
    if current["status"] != "pending" or proposal["context"] != action_scope(body.page_context):
        raise ApiError(409, "ACTION_STALE", "이 화면에서 실행할 수 없는 제안입니다. 현재 화면에서 다시 요청해 주세요.")
    flow = run.result.get("workflow")
    flow_status = effective_state(run, flow, proposal, now) if flow else None
    choosing = flow_status == "WAITING_USER_INPUT" and proposal.get("requires_selection")
    if flow and (run.status != RunStatus.SUCCEEDED or not (flow_status == "WAITING_APPROVAL" or choosing)):
        raise ApiError(409, "ACTION_STALE", "현재 단계에서 실행할 수 없는 제안입니다.")
    if body.decision == "select":
        if not choosing:
            raise ApiError(409, "WORKFLOW_SELECTION_UNAVAILABLE", "현재 단계에서는 문서를 선택할 수 없습니다.")
        scoped_context(db, user.id, body.page_context)
        execution_command(db, proposal, body.page_context, body.sop_id)
        select_sop(run, proposal, body.sop_id, now)
        db.commit()
        log_action(run, proposal, "selected", target=body.sop_id)
        return {"status": "selected", "command": None}
    command = None
    if body.decision == "approve":
        if proposal.get("requires_selection") and (choosing or proposal.get("chosen_sop_id") != str(body.sop_id)):
            raise ApiError(409, "WORKFLOW_SELECTION_REQUIRED", "선택한 문서만 열 수 있습니다. 먼저 문서를 선택해 주세요.")
        scoped_context(db, user.id, body.page_context)
        command = execution_command(db, proposal, body.page_context, body.sop_id)
    status = "approved" if command else "cancelled"
    run.result = {**run.result, "action_proposal": {**proposal, "status": status, "decided_at": now.isoformat(),
                                                   "selected_sop_id": str(body.sop_id) if command and body.sop_id else None}}
    if flow and status == "cancelled":
        pause_or_cancel(run, "CANCELLED", now)
    db.commit()
    log_action(run, proposal, status, target=body.sop_id if command else None)
    return {"status": status, "command": command}


@router.post("/{conversation_id}/actions/{message_id}/feedback", status_code=204)
def action_feedback(conversation_id: UUID, message_id: UUID, body: ActionFeedback,
                    db: Database, user: WriteUser, request: Request) -> None:
    require_trainee(user)
    lock_chat(db, user.id)
    run = db.scalar(conversation_rows(user.id, conversation_id).where(AgentRun.id == message_id).with_for_update())
    if run and run.companion.get("action_session") == session_binding(request):
        workflow = (run.result or {}).get("workflow") or {}
        if any(receipt == [str(body.proposal_id), str(body.view_id), body.outcome, body.error_code]
               for receipt in workflow.get("feedback", [])):
            return
    run, proposal = locked_proposal(db, user, request, conversation_id, message_id, body)
    if proposal["status"] == body.outcome and proposal.get("error_code") == body.error_code:
        return
    if proposal["status"] != "approved":
        raise ApiError(409, "ACTION_NOT_APPROVED", "승인된 실행의 결과만 기록할 수 있습니다.")
    now = utcnow()
    if run.result.get("workflow") and now >= datetime.fromisoformat(proposal["expires_at"]):
        raise ApiError(409, "ACTION_STALE", "실행 제안이 만료되었습니다. 현재 화면에서 작업을 재개해 주세요.")
    run.result = {**run.result, "action_proposal": {**proposal, "status": body.outcome, "error_code": body.error_code,
                                                   "completed_at": now.isoformat()}}
    if run.result.get("workflow"):
        after_feedback(db, run, proposal, body, now)
    db.commit()
    log_action(run, proposal, body.outcome, target=proposal.get("selected_sop_id"), error=body.error_code)


@router.post("/{conversation_id}/workflows/{message_id}")
def workflow_control(conversation_id: UUID, message_id: UUID, body: WorkflowControl,
                     db: Database, user: WriteUser, request: Request, response: Response) -> dict:
    require_trainee(user)
    response.headers["Cache-Control"] = "no-store"
    lock_chat(db, user.id)
    run = db.scalar(conversation_rows(user.id, conversation_id).where(AgentRun.id == message_id).with_for_update())
    flow = (run.result or {}).get("workflow") if run else None
    if not flow or run.companion.get("action_session") != session_binding(request):
        raise ApiError(404, "WORKFLOW_NOT_FOUND", "이 로그인 세션의 작업을 찾을 수 없습니다.")
    now = utcnow()
    status = effective_state(run, flow, run.result.get("action_proposal"), now)
    if body.command == "resume":
        if active_workflow(db, user.id, now, except_id=run.id):
            raise ApiError(409, "WORKFLOW_BUSY", "다른 워크플로가 진행 중입니다.")
        scoped_context(db, user.id, body.page_context)
        queue_resume(run, body, now, clean_text(body.clue, 160) if body.clue else None)
    elif body.command == "cancel" and status == "CANCELLED":
        return turn_view(db, run)
    elif status in {*WORKFLOW_ACTIVE, "FAILED"}:
        selecting = status == "WAITING_USER_INPUT" and (run.result.get("action_proposal") or {}).get("requires_selection")
        if body.command == "pause" and status not in {"PLANNING", "RUNNING", "WAITING_APPROVAL", "PAUSED"} and not selecting:
            raise ApiError(409, "WORKFLOW_STATE", "현재 작업은 일시중지할 수 없습니다.")
        pause_or_cancel(run, "CANCELLED" if body.command == "cancel" else "PAUSED", now, reason="WORKFLOW_USER_PAUSED")
    else:
        raise ApiError(409, "WORKFLOW_STATE", "이미 종료된 작업입니다.")
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "uq_agent_runs_active_user":
            raise
        raise ApiError(409, "AGENT_BUSY", "현재 처리 중인 질문이나 분석이 끝난 뒤 다시 시도해 주세요.") from exc
    return turn_view(db, run)


@router.delete("/{conversation_id}", status_code=204)
def reset_chat(conversation_id: UUID, db: Database, user: WriteUser) -> None:
    require_trainee(user)
    lock_chat(db, user.id)
    turns = db.scalars(conversation_rows(user.id, conversation_id).with_for_update()).all()
    for turn in turns:
        clear_transcript(turn)
    db.commit()
