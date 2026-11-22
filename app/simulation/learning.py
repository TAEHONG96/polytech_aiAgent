from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agent.schemas import StrictInput
from app.db import Database
from app.http import ApiError
from app.knowledge.models import SopDocument
from app.knowledge.search import incident_scope
from app.models import Ticket, TicketEvent
from app.simulation.access import WritableRun
from app.simulation.models import LearningEvent, ScenarioIncident, SimulationRun
from app.tickets import ticket_by_id

router = APIRouter(prefix="/simulation-runs/{run_id}", tags=["training evidence"])
APPLIED_ACTIONS = {
    "CHECK_SERVER_STATUS", "RUN_BMC_DIAGNOSTICS", "CHECK_MEMORY", "CHECK_STORAGE", "REPLACE_MEMORY", "RERUN_DIAGNOSTICS",
}


class LearningInput(StrictInput):
    kind: Literal["HOST_HISTORY", "SOP"]
    activity: Literal["VIEWED", "USED"]
    source_id: UUID
    action_event_id: UUID | None = None


def learning_source(db: Session, ticket: Ticket, kind: str, source_id: UUID) -> bool:
    if kind == "HOST_HISTORY":
        query = select(ScenarioIncident.id).where(
            ScenarioIncident.id == source_id, *incident_scope(ticket),
            ScenarioIncident.scenario_host_id == ticket.scenario_host_id,
        )
    elif kind == "SOP":
        query = select(SopDocument.id).where(SopDocument.id == source_id, SopDocument.training_approved.is_(True))
    else:
        return False
    return db.scalar(query) is not None


@router.post("/tickets/{ticket_id}/learning-evidence", status_code=201)
def record_learning(ticket_id: UUID, body: LearningInput, response: Response, db: Database, run: WritableRun) -> dict:
    ticket = ticket_by_id(db, ticket_id, run_id=run.id)
    if not learning_source(db, ticket, body.kind, body.source_id):
        raise ApiError(404, "EVIDENCE_NOT_FOUND", "이 티켓에 허용된 학습 자료를 찾을 수 없습니다.")
    if (body.activity == "USED") != (body.action_event_id is not None):
        raise ApiError(422, "INVALID_LEARNING_EVIDENCE", "활용 기록에는 실제 가상 조치가 필요합니다.")
    query = select(LearningEvent).where(
        LearningEvent.simulation_run_id == run.id, LearningEvent.ticket_id == ticket.id,
        LearningEvent.source_id == body.source_id, LearningEvent.kind == body.kind,
    )
    existing = db.scalar(query.where(LearningEvent.activity == body.activity))
    if existing is not None:
        if existing.action_event_id != body.action_event_id:
            raise ApiError(409, "LEARNING_EVIDENCE_RECORDED", "이 자료의 활용을 이미 기록했습니다.")
        response.status_code = 200
        return {"id": existing.id, "event_sequence": existing.event_sequence, "activity": existing.activity}
    if body.activity == "USED":
        viewed = db.scalar(query.where(LearningEvent.activity == "VIEWED"))
        action = db.scalar(select(TicketEvent).where(
            TicketEvent.id == body.action_event_id, TicketEvent.simulation_run_id == run.id,
            TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "virtual_action_performed",
            TicketEvent.actor_id == run.user_id,
        ))
        if (viewed is None or action is None or action.event_sequence <= viewed.event_sequence
                or action.details.get("action_code") not in APPLIED_ACTIONS):
            raise ApiError(422, "INVALID_LEARNING_EVIDENCE", "자료를 확인한 뒤 수행한 가상 조치를 연결하세요.")
    sequence = db.scalar(update(SimulationRun).where(SimulationRun.id == run.id).values(
        last_event_sequence=SimulationRun.last_event_sequence + 1,
    ).returning(SimulationRun.last_event_sequence))
    entry = LearningEvent(simulation_run_id=run.id, ticket_id=ticket.id, actor_id=run.user_id,
                          event_sequence=sequence, occurred_at=datetime.now(timezone.utc), kind=body.kind,
                          source_id=body.source_id, activity=body.activity, action_event_id=body.action_event_id)
    db.add(entry)
    db.flush()
    result = {"id": entry.id, "event_sequence": sequence, "activity": entry.activity}
    db.commit()
    return result
