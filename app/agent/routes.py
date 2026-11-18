from uuid import UUID

from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.models import AgentRun, AgentToolCall
from app.agent.queue import ACTIVE_RUNS, utcnow
from app.agent.schemas import MAX_MESSAGE, MAX_TOOL_CALLS, AnalyzeInput, RunStatus, clean_text
from app.auth import CurrentUser, WriteUser
from app.db import Database
from app.http import ApiError
from app.models import Ticket

router = APIRouter(prefix="/agent", tags=["agent"])


def run_view(run: AgentRun, calls: list[AgentToolCall]) -> dict:
    return {
        "run_id": run.id, "status": run.status, "message": run.message, "ticket_id": run.ticket_id,
        "provider": run.provider, "model": run.model, "created_at": run.created_at,
        "started_at": run.started_at, "finished_at": run.finished_at, "result": run.result,
        "error": {"code": run.error_code, "message": run.error_message, "retryable": run.retryable} if run.error_code else None,
        "usage": run.usage,
        "tool_limit": MAX_TOOL_CALLS,
        "tool_calls": [
            {"sequence": event.sequence, "name": event.name, "status": event.status, "details": event.details,
             "elapsed_ms": event.elapsed_ms, "created_at": event.created_at}
            for event in calls
        ],
    }


@router.post("/analyze", status_code=202)
def analyze(body: AnalyzeInput, response: Response, db: Database, user: WriteUser) -> dict:
    if body.ticket_id is not None and db.get(Ticket, body.ticket_id) is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
    user_id = user.id
    run = AgentRun(
        requested_by=user_id, ticket_id=body.ticket_id, message=clean_text(body.message, MAX_MESSAGE),
        status=RunStatus.QUEUED, created_at=utcnow(),
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "uq_agent_runs_active_user":
            raise
        active = db.scalar(select(AgentRun.id).where(
            AgentRun.requested_by == user_id, AgentRun.status.in_(ACTIVE_RUNS)
        ))
        raise ApiError(409, "AGENT_BUSY", "진행 중인 분석을 먼저 확인하세요.",
                       {"run_id": str(active)} if active is not None else None) from exc
    response.headers["Location"] = f"/api/v1/agent/runs/{run.id}"
    return run_view(run, [])


@router.get("/runs/{run_id}")
def read_run(run_id: UUID, db: Database, user: CurrentUser) -> dict:
    run = db.scalar(select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.requested_by == user.id, AgentRun.handover_id.is_(None),
    ))
    if run is None:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "분석 실행을 찾을 수 없습니다.")
    events = list(db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id).order_by(AgentToolCall.sequence)))
    return run_view(run, events)
