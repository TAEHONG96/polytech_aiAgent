from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.db import Database
from app.handover.models import Handover, TicketWork
from app.http import ApiError
from app.knowledge.models import SopDocument
from app.knowledge.search import incident_scope
from app.models import Ticket, TicketEvent
from app.simulation.access import OwnedRun
from app.simulation.models import ScenarioIncident, ScenarioTicketTemplate, SimulationEvaluation, SimulationTicketEvaluation

router = APIRouter(prefix="/simulation-runs/{run_id}/evaluation", tags=["training evaluation"])


def evaluation_for_run(db: Database, run: OwnedRun) -> SimulationEvaluation:
    if run.status != "COMPLETED":
        raise ApiError(409, "SIMULATION_NOT_COMPLETED", "완료된 실습의 평가를 조회하세요.")
    evaluation = db.scalar(select(SimulationEvaluation).join(
        Handover, Handover.id == SimulationEvaluation.handover_id,
    ).where(SimulationEvaluation.simulation_run_id == run.id,
            Handover.simulation_run_id == run.id, Handover.created_by == run.user_id))
    if evaluation is None:
        raise ApiError(404, "EVALUATION_NOT_FOUND", "실습 평가를 찾을 수 없습니다.")
    return evaluation


def evaluation_tickets(evaluation: SimulationEvaluation):
    return select(SimulationTicketEvaluation, Ticket, ScenarioTicketTemplate.template_key).join(
        Ticket, Ticket.id == SimulationTicketEvaluation.ticket_id,
    ).join(ScenarioTicketTemplate, ScenarioTicketTemplate.id == Ticket.scenario_ticket_template_id).where(
        SimulationTicketEvaluation.evaluation_id == evaluation.id,
        Ticket.simulation_run_id == evaluation.simulation_run_id, Ticket.training_origin.in_(("INITIAL", "CARRYOVER")),
    )


def statuses(evaluation: SimulationEvaluation) -> dict:
    return {"rules_status": evaluation.rules_status, "explanation_status": evaluation.explanation_status}

Evaluation = Annotated[SimulationEvaluation, Depends(evaluation_for_run)]


@router.get("")
def evaluate(db: Database, evaluation: Evaluation) -> dict:
    rows = db.execute(evaluation_tickets(evaluation)).all()
    resolved = sum(row[0].final_status in ("resolved", "closed") for row in rows)
    return {"run_id": evaluation.simulation_run_id, **statuses(evaluation),
            "evaluated_ticket_counts": {"total": len(rows), "resolved": resolved, "unresolved": len(rows) - resolved}}


def ticket_summary(row, evaluation: SimulationEvaluation) -> dict:
    entry, ticket, template_key = row
    return {"ticket_id": ticket.id, "ticket_key": template_key, "training_origin": ticket.training_origin,
            "activity_status": entry.activity_status, "final_status": entry.final_status, **statuses(evaluation)}


@router.get("/tickets")
def evaluated_tickets(db: Database, evaluation: Evaluation) -> dict:
    rows = db.execute(evaluation_tickets(evaluation).order_by(Ticket.number))
    return {"items": [ticket_summary(row, evaluation) for row in rows]}


def is_authorized_ref(db: Database, kind: str, value: UUID, ticket: Ticket) -> bool:
    if kind == "event":
        query = select(TicketEvent.id).where(TicketEvent.id == value, TicketEvent.ticket_id == ticket.id)
    elif kind == "work":
        query = select(TicketWork.id).where(TicketWork.id == value, TicketWork.ticket_id == ticket.id)
    elif kind == "sop":
        query = select(SopDocument.id).where(SopDocument.id == value, SopDocument.training_approved.is_(True))
    elif kind == "incident":
        query = select(ScenarioIncident.id).where(ScenarioIncident.id == value, *incident_scope(ticket))
    else:
        return False
    return db.scalar(query) is not None


@router.get("/tickets/{ticket_id}")
def evaluated_ticket(ticket_id: UUID, db: Database, evaluation: Evaluation) -> dict:
    row = db.execute(evaluation_tickets(evaluation).where(Ticket.id == ticket_id)).one_or_none()
    if row is None:
        raise ApiError(404, "EVALUATION_TICKET_NOT_FOUND", "실습 티켓 평가를 찾을 수 없습니다.")
    entry, ticket, _ = row
    approved = set()
    for ref in entry.evidence or []:
        try:
            if is_authorized_ref(db, ref["kind"], UUID(ref["id"]), ticket):
                approved.add(ref["id"])
        except (TypeError, ValueError, KeyError):
            continue
    rules = None
    if evaluation.rules_status == "READY":
        rules = [{"rule_code": rule["rule_code"], "result": rule["result"], "evidence_refs": rule.get("evidence_refs", [])}
                 for rule in entry.rule_results or [] if set(rule.get("evidence_refs", [])) <= approved]
    feedback = entry.explanation if (evaluation.explanation_status == "READY"
                                     and len(approved) == len(entry.evidence or [])) else None
    return {**ticket_summary(row, evaluation), "rule_results": rules, "feedback": feedback}
