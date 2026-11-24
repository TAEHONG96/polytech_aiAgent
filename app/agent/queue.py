import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import UUID, uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.agent.analysis import Trace, Usage, investigate
from app.agent.models import AgentRun, AgentToolCall
from app.agent.provider import AnalysisFailure, generator
from app.agent.schemas import QUEUE_SECONDS, RUN_SECONDS, RunStatus
from app.agent.tools import Toolbox
from app.db import engine
from app.handover.analysis import draft_handover
from app.handover.models import Handover, HandoverRevision, ShiftInstance, ShiftMembership
from app.handover.service import snapshot_items
from app.simulation.access import worker_scope
from app.simulation.models import ScenarioShiftSlot, SimulationRun
from app.tickets import ticket_by_id
from app.http import ApiError

logger = logging.getLogger(__name__)
chat_logger = logging.getLogger("uvicorn.error.companion")
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
    handover_id: UUID | None
    handover_version: int | None
    simulation_run_id: UUID | None
    companion: dict | None = None


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
        flow = (run.result or {}).get("workflow")
        queued_at = datetime.fromisoformat(flow["queued_at"]) if flow else run.created_at
        deadline = min(queued_at + timedelta(seconds=QUEUE_SECONDS), now + timedelta(seconds=RUN_SECONDS))
        if flow:
            deadline = min(deadline, datetime.fromisoformat(flow["expires_at"]))
        if deadline <= now:
            fail_run(run, AnalysisFailure("AI_QUEUE_EXPIRED", "분석 요청 대기 시간이 초과되었습니다.", retryable=True))
            return None
        run.status = RunStatus.RUNNING
        run.started_at = now
        run.lease_token = uuid4()
        run.lease_expires_at = deadline + timedelta(seconds=20)
        return Work(run.id, run.requested_by, run.ticket_id, run.message, run.lease_token, deadline,
                    run.handover_id, run.handover_version, run.simulation_run_id, run.companion)


def owned_run(db: Session, work: Work) -> AgentRun:
    run = db.get(AgentRun, work.id, with_for_update=True)
    if (run is None or run.status != RunStatus.RUNNING or run.lease_token != work.token
            or (run.requested_by, run.ticket_id, run.handover_id, run.handover_version, run.simulation_run_id)
            != (work.requested_by, work.ticket_id, work.handover_id, work.handover_version, work.simulation_run_id)):
        raise RunLost()
    return run


def record_trace(work: Work, event: Trace) -> None:
    with Session(engine()) as db, db.begin():
        owned_run(db, work)
        db.add(AgentToolCall(
            run_id=work.id, sequence=event.sequence, name=event.name,
            status=event.status, details=event.details, elapsed_ms=event.elapsed_ms, created_at=utcnow(),
        ))


def authorized_handover(db: Session, work: Work) -> Handover:
    report = db.scalar(select(Handover).where(Handover.id == work.handover_id,
                                             Handover.simulation_run_id == work.simulation_run_id))
    if report is None or db.get(HandoverRevision, (report.id, work.handover_version)) is None:
        raise PermissionError("Invalid handover revision scope")
    if work.simulation_run_id is not None:
        if report.created_by != work.requested_by:
            raise PermissionError("Invalid training report owner")
    else:
        shift = db.get(ShiftInstance, report.outgoing_shift_id)
        if shift is None or not db.get(ShiftMembership, (shift.team_id, work.requested_by)):
            raise PermissionError("Outgoing shift membership required")
    return report


async def execute(work: Work, model, usage: Usage) -> dict:
    try:
        seconds = (work.deadline - utcnow()).total_seconds()
        if seconds <= 0:
            raise AnalysisFailure("AI_TIMEOUT", "분석 실행 시간이 초과되었습니다.", retryable=True)
        if work.companion is not None:
            if work.companion.get("workflow_kind"):
                from app.companion.workflow_worker import run_workflow
                return await run_workflow(work, model, usage)
            from app.companion.service import answer_chat
            return await answer_chat(work, model, usage, seconds=seconds, now=utcnow())
        if work.handover_id is not None:
            with Session(engine()) as db:
                worker_scope(db, work.requested_by, work.simulation_run_id)
                report = authorized_handover(db, work)
                shift_id = report.outgoing_shift_id
                if work.simulation_run_id is not None:
                    scenario_id = db.get(SimulationRun, work.simulation_run_id).scenario_id
                    shift_id = db.scalar(select(ScenarioShiftSlot.id).where(
                        ScenarioShiftSlot.scenario_id == scenario_id, ScenarioShiftSlot.slot_order == 1,
                    ))
                items = snapshot_items(db, report.id, work.handover_version, run_id=work.simulation_run_id)
            return await draft_handover(shift_id=shift_id, items=items, user_id=work.requested_by, model=model,
                                        usage=usage, seconds=seconds, trace=lambda event: record_trace(work, event),
                                        run_id=work.simulation_run_id)
        return await investigate(
            request={"message": work.message, "ticket_id": str(work.ticket_id) if work.ticket_id else None},
            toolbox=Toolbox(work.requested_by, work.ticket_id, run_id=work.simulation_run_id),
            model=model, usage=usage, seconds=seconds,
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
    started = time.monotonic()
    result = None
    failure = None
    model = None
    answered_from_records = False
    try:
        with Session(engine()) as db:
            if work.companion is None or work.simulation_run_id is not None:
                worker_scope(db, work.requested_by, work.simulation_run_id)
            if work.ticket_id is not None:
                try:
                    ticket_by_id(db, work.ticket_id, run_id=work.simulation_run_id)
                except ApiError as exc:
                    raise PermissionError("Invalid analysis ticket scope") from exc
            if work.handover_id is not None:
                if work.ticket_id is not None:
                    raise PermissionError("Invalid analysis parents")
                authorized_handover(db, work)
            elif work.companion is None and work.simulation_run_id is not None and work.ticket_id is None:
                raise PermissionError("Invalid training analysis scope")
        if work.companion is not None and not work.companion.get("workflow_kind"):
            from app.companion.service import answer_without_model
            result = answer_without_model(work)
            answered_from_records = result is not None
        if not answered_from_records:
            model = (generator(default_provider="gemini")
                     if work.companion is not None and model_factory is generator else model_factory())
            with Session(engine()) as db, db.begin():
                run = owned_run(db, work)
                run.provider = model.provider
                run.model = model.model
            result = asyncio.run(execute(work, model, usage))
    except RunLost:
        return True
    except PermissionError:
        failure = AnalysisFailure("AI_UNAUTHORIZED", "더 이상 분석을 실행할 수 없는 계정입니다.")
    except ApiError:
        failure = AnalysisFailure("CHAT_CONTEXT_UNAVAILABLE", "현재 업무 데이터에 접근할 수 없습니다.")
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
                if answered_from_records:
                    run.provider = "workflow"
                run.result = result
                run.finished_at = utcnow()
                run.lease_token = None
                run.lease_expires_at = None
        if work.companion is not None:
            chat_logger.info("companion_chat run=%s provider=%s status=%s elapsed_ms=%d model_attempts=%d embedding_requests=%d code=%s",
                             work.id, model.provider if model else "workflow" if answered_from_records else "unavailable",
                             "failed" if failure else "succeeded", round((time.monotonic() - started) * 1000),
                             usage.model_requests, usage.embedding_requests, failure.code if failure else "ok")
    except RunLost:
        pass
    return True


def process_agent_queue(stop: Event) -> None:
    from app.companion.service import CHAT_TTL
    next_cleanup = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + 300
                with Session(engine()) as db, db.begin():
                    db.execute(update(AgentRun).where(
                        AgentRun.companion["conversation_id"].astext.is_not(None),
                        AgentRun.created_at < utcnow() - CHAT_TTL, AgentRun.status.not_in(ACTIVE_RUNS),
                    ).values(message="", result=None, ticket_id=None, simulation_run_id=None, companion={"expired": True}))
            if process_agent_run():
                continue
        except Exception as exc:
            logger.warning("agent worker iteration failed type=%s", type(exc).__name__)
        stop.wait(1)
