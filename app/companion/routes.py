from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent.models import AgentRun
from app.agent.queue import ACTIVE_RUNS, utcnow
from app.agent.schemas import MAX_MESSAGE, RunStatus, clean_text
from app.auth import SESSION_COOKIE, CurrentUser, WriteUser, csrf_value
from app.companion.actions import action_scope, execution_command, log_action, proposal_view
from app.companion.schemas import ActionDecision, ActionFeedback, ActionView, ChatInput, Hint, PageContext
from app.companion.service import CHAT_TTL, PAGE_LABELS, clear_transcript, require_trainee, scoped_context, scoped_result
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
    status = run.status
    error = {"code": run.error_code, "message": run.error_message, "retryable": run.retryable} if run.error_code else None
    try:
        info = scoped_result(db, run.requested_by, context, result)
        info = result.get("context_info", info)
    except ApiError:
        info = {"page_id": context.page_id, "label": PAGE_LABELS[context.page_id]}
        status, result = RunStatus.FAILED, {}
        error = {"code": "CHAT_CONTEXT_UNAVAILABLE", "message": "이 질문의 업무 데이터 또는 출처에 접근할 수 없습니다.", "retryable": False}
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
            "provider": run.provider, "model": run.model, "execution_mode": mode,
            "action_proposal": proposal_view(run, result, utcnow())}


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
    previous = db.scalar(conversation_rows(user.id, body.conversation_id).where(
        AgentRun.companion["request_id"].astext == str(body.request_id)))
    if previous:
        prior = {key: value for key, value in previous.companion.items() if key != "action_view_closed"}
        if previous.message != message or prior != metadata:
            raise ApiError(409, "CHAT_REQUEST_CONFLICT", "같은 요청 ID에 다른 질문을 보낼 수 없습니다.")
        return turn_view(db, previous)
    now = utcnow()
    count = db.scalar(select(func.count()).select_from(AgentRun).where(
        AgentRun.requested_by == user.id, AgentRun.companion.is_not(None), AgentRun.created_at >= now - timedelta(minutes=1)))
    if count >= 8:
        raise ApiError(429, "CHAT_RATE_LIMIT", "키로 질문이 많습니다. 잠시 뒤 다시 시도하세요.")
    run = AgentRun(requested_by=user.id, ticket_id=context.ticket_id, message=message, simulation_run_id=context.run_id,
                   status=RunStatus.QUEUED, created_at=now, companion=metadata)
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
def invalidate_view(conversation_id: UUID, body: ActionView, db: Database, user: WriteUser, request: Request) -> None:
    require_trainee(user)
    lock_chat(db, user.id)
    rows = db.scalars(conversation_rows(user.id, conversation_id).where(
        AgentRun.companion["action_view_id"].astext == str(body.view_id),
        AgentRun.companion["action_session"].astext == session_binding(request),
    ).with_for_update())
    for run in rows:
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
    command = None
    if body.decision == "approve":
        scoped_context(db, user.id, body.page_context)
        command = execution_command(db, proposal, body.page_context, body.sop_id)
    status = "approved" if command else "cancelled"
    run.result = {**run.result, "action_proposal": {**proposal, "status": status, "decided_at": now.isoformat(),
                                                   "selected_sop_id": str(body.sop_id) if command and body.sop_id else None}}
    db.commit()
    log_action(run, proposal, status, target=body.sop_id if command else None)
    return {"status": status, "command": command}


@router.post("/{conversation_id}/actions/{message_id}/feedback", status_code=204)
def action_feedback(conversation_id: UUID, message_id: UUID, body: ActionFeedback,
                    db: Database, user: WriteUser, request: Request) -> None:
    run, proposal = locked_proposal(db, user, request, conversation_id, message_id, body)
    if proposal["status"] == body.outcome and proposal.get("error_code") == body.error_code:
        return
    if proposal["status"] != "approved":
        raise ApiError(409, "ACTION_NOT_APPROVED", "승인된 실행의 결과만 기록할 수 있습니다.")
    run.result = {**run.result, "action_proposal": {**proposal, "status": body.outcome, "error_code": body.error_code,
                                                   "completed_at": utcnow().isoformat()}}
    db.commit()
    log_action(run, proposal, body.outcome, target=proposal.get("selected_sop_id"), error=body.error_code)


@router.delete("/{conversation_id}", status_code=204)
def reset_chat(conversation_id: UUID, db: Database, user: WriteUser) -> None:
    require_trainee(user)
    lock_chat(db, user.id)
    turns = db.scalars(conversation_rows(user.id, conversation_id).with_for_update()).all()
    for turn in turns:
        clear_transcript(turn)
    db.commit()
