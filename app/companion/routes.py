from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent.models import AgentRun
from app.agent.queue import ACTIVE_RUNS, utcnow
from app.agent.schemas import MAX_MESSAGE, RunStatus, clean_text
from app.auth import CurrentUser, WriteUser
from app.companion.schemas import ChatInput, Hint, PageContext
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
    return {"conversation_id": run.companion["conversation_id"], "message_id": run.id,
            "request_id": run.companion["request_id"], "message": run.message,
            "active_hint_id": run.companion.get("active_hint_id"), "status": status,
            "context_info": info, "timestamp": run.created_at,
            "answer": result.get("answer"), "sources": result.get("sources", []),
            "retrieval": result.get("retrieval"), "error": error, "provider": run.provider}


@router.post("", status_code=202)
def chat(body: ChatInput, db: Database, user: WriteUser, response: Response) -> dict:
    require_trainee(user)
    response.headers["Cache-Control"] = "no-store"
    context = body.page_context
    _, facts, _ = scoped_context(db, user.id, context)
    lock_chat(db, user.id)
    message = clean_text(body.message, MAX_MESSAGE)
    metadata = {"conversation_id": str(body.conversation_id), "request_id": str(body.request_id),
                "page_context": context.model_dump(mode="json"), "active_hint_id": valid_hint(body, facts)}
    previous = db.scalar(conversation_rows(user.id, body.conversation_id).where(
        AgentRun.companion["request_id"].astext == str(body.request_id)))
    if previous:
        if previous.message != message or previous.companion != metadata:
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


@router.delete("/{conversation_id}", status_code=204)
def reset_chat(conversation_id: UUID, db: Database, user: WriteUser) -> None:
    require_trainee(user)
    lock_chat(db, user.id)
    turns = db.scalars(conversation_rows(user.id, conversation_id).with_for_update()).all()
    for turn in turns:
        clear_transcript(turn)
    db.commit()
