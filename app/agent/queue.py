import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agent.analysis import Trace, Usage, investigate
from app.agent.models import AgentRun, AgentToolCall
from app.agent.provider import AnalysisFailure, generator
from app.agent.schemas import QUEUE_SECONDS, RUN_SECONDS, RunStatus
from app.agent.tools import Toolbox, require_active_user
from app.db import engine

logger = logging.getLogger(__name__)
ACTIVE_RUNS = (RunStatus.QUEUED, RunStatus.RUNNING)


def utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Work:
    id: UUID
    requested_by: UUID
    ticket_id: UUID | None
    message: str
    token: UUID
    deadline: datetime


class RunLost(Exception):
    pass


def fail_run(run: AgentRun, error: AnalysisFailure) -> None:
    run.status = RunStatus.FAILED
    run.error_code = error.code
    run.error_message = str(error)
    run.retryable = error.retryable
    run.finished_at = utcnow()
    run.lease_token = None
    run.lease_expires_at = None


def claim_run() -> Work | None:
    now = utcnow()
    with Session(engine()) as db, db.begin():
        run = db.scalar(
            select(AgentRun).where(or_(
                AgentRun.status == RunStatus.QUEUED,
                (AgentRun.status == RunStatus.RUNNING) & (AgentRun.lease_expires_at <= now),
            )).order_by(AgentRun.created_at, AgentRun.id).limit(1).with_for_update(skip_locked=True)
        )
        if run is None:
            return None
        if run.status == RunStatus.RUNNING:
            fail_run(run, AnalysisFailure("AI_WORKER_INTERRUPTED", "분석이 중단되었습니다. 다시 시도할 수 있습니다.", retryable=True))
            return None
        deadline = min(run.created_at + timedelta(seconds=QUEUE_SECONDS), now + timedelta(seconds=RUN_SECONDS))
        if deadline <= now:
            fail_run(run, AnalysisFailure("AI_QUEUE_EXPIRED", "분석 요청 대기 시간이 초과되었습니다.", retryable=True))
            return None
        run.status = RunStatus.RUNNING
        run.started_at = now
        run.lease_token = uuid4()
        run.lease_expires_at = deadline + timedelta(seconds=20)
        return Work(run.id, run.requested_by, run.ticket_id, run.message, run.lease_token, deadline)


def owned_run(db: Session, work: Work) -> AgentRun:
    run = db.get(AgentRun, work.id, with_for_update=True)
    if run is None or run.status != RunStatus.RUNNING or run.lease_token != work.token:
        raise RunLost()
    return run


def record_trace(work: Work, event: Trace) -> None:
    with Session(engine()) as db, db.begin():
        owned_run(db, work)
        db.add(AgentToolCall(
            run_id=work.id, sequence=event.sequence, name=event.name,
            status=event.status, details=event.details, elapsed_ms=event.elapsed_ms, created_at=utcnow(),
        ))


async def execute(work: Work, model, usage: Usage) -> dict:
    try:
        seconds = (work.deadline - utcnow()).total_seconds()
        if seconds <= 0:
            raise AnalysisFailure("AI_TIMEOUT", "분석 실행 시간이 초과되었습니다.", retryable=True)
        return await investigate(
            request={"message": work.message, "ticket_id": str(work.ticket_id) if work.ticket_id else None},
            toolbox=Toolbox(work.requested_by, work.ticket_id), model=model, usage=usage, seconds=seconds,
            trace=lambda event: record_trace(work, event),
        )
    finally:
        try:
            async with asyncio.timeout(2):
                await model.close()
        except Exception as exc:
            logger.warning("agent client close failed type=%s", type(exc).__name__)


def process_agent_run(model_factory=generator) -> bool:
    work = claim_run()
    if work is None:
        return False

    usage = Usage()
    result = None
    failure = None
    model = None
    try:
        with Session(engine()) as db:
            require_active_user(db, work.requested_by)
        model = model_factory()
        with Session(engine()) as db, db.begin():
            run = owned_run(db, work)
            run.provider = model.provider
            run.model = model.model
        result = asyncio.run(execute(work, model, usage))
    except RunLost:
        return True
    except PermissionError:
        failure = AnalysisFailure("AI_UNAUTHORIZED", "더 이상 분석을 실행할 수 없는 계정입니다.")
    except AnalysisFailure as exc:
        failure = exc
    except Exception as exc:
        logger.warning("agent run error run=%s type=%s", work.id, type(exc).__name__)
        failure = AnalysisFailure("AI_UNAVAILABLE", "분석 처리 과정에서 오류가 발생했습니다.", retryable=True)

    try:
        with Session(engine()) as db, db.begin():
            run = owned_run(db, work)
            run.usage = asdict(usage)
            if failure:
                fail_run(run, failure)
            else:
                run.status = RunStatus.SUCCEEDED
                run.result = result
                run.finished_at = utcnow()
                run.lease_token = None
                run.lease_expires_at = None
    except RunLost:
        pass
    return True


def process_agent_queue(stop: Event) -> None:
    while not stop.is_set():
        try:
            if process_agent_run():
                continue
        except Exception as exc:
            logger.warning("agent worker iteration failed type=%s", type(exc).__name__)
        stop.wait(1)
