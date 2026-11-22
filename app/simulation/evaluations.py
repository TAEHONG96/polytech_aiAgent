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
from app.simulation.learning import learning_source
from app.simulation.models import LearningEvent, ScenarioIncident, ScenarioTicketTemplate, SimulationEvaluation, SimulationTicketEvaluation

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
    return {"run_id": evaluation.simulation_run_id, "evaluation_id": evaluation.id, **statuses(evaluation),
            "rule_summary": evaluation.rule_summary if evaluation.rules_status == "READY" else None,
            "last_event_sequence": evaluation.input_snapshot.get("last_event_sequence"),
            "warnings": evaluation.input_snapshot.get("warnings", []),
            "evaluated_ticket_counts": {"total": len(rows), "resolved": resolved, "unresolved": len(rows) - resolved}}


def ticket_summary(row, evaluation: SimulationEvaluation) -> dict:
    entry, ticket, template_key = row
    return {"ticket_id": ticket.id, "ticket_key": template_key, "title": ticket.title, "training_origin": ticket.training_origin,
            "activity_status": entry.activity_status, "final_status": entry.final_status, **statuses(evaluation)}


@router.get("/tickets")
def evaluated_tickets(db: Database, evaluation: Evaluation) -> dict:
    rows = db.execute(evaluation_tickets(evaluation).order_by(Ticket.number))
    return {"items": [ticket_summary(row, evaluation) for row in rows]}


def evidence_url(db: Database, kind: str, value: UUID, ticket: Ticket, evaluation: SimulationEvaluation) -> str | None:
    base = f"/api/v1/simulation-runs/{evaluation.simulation_run_id}"
    path = f"{base}/tickets/{ticket.id}"
    if kind == "event":
        query = select(TicketEvent.id).where(TicketEvent.id == value, TicketEvent.ticket_id == ticket.id)
        url = path + "/operations"
    elif kind == "work":
        query = select(TicketWork.id).where(TicketWork.id == value, TicketWork.ticket_id == ticket.id)
        url = path + "/operations"
    elif kind == "sop":
        query = select(SopDocument.id).where(SopDocument.id == value, SopDocument.training_approved.is_(True))
        url = f"{base}/sops/{value}"
    elif kind == "incident":
        query = select(ScenarioIncident.id).where(ScenarioIncident.id == value, *incident_scope(ticket))
        url = f"{path}/incident-sources/{value}"
    elif kind == "learning":
        activity = db.scalar(select(LearningEvent).where(
            LearningEvent.id == value, LearningEvent.ticket_id == ticket.id,
            LearningEvent.simulation_run_id == evaluation.simulation_run_id,
        ))
        if activity is None or not learning_source(db, ticket, activity.kind, activity.source_id):
            return None
        return (f"{path}/incident-sources/{activity.source_id}" if activity.kind == "HOST_HISTORY"
                else f"{base}/sops/{activity.source_id}")
    elif kind == "handover":
        return f"{base}/outgoing-handover/revisions/{evaluation.handover_revision}" if value == evaluation.handover_id else None
    else:
        return None
    return url if db.scalar(query) is not None else None


@router.get("/tickets/{ticket_id}")
def evaluated_ticket(ticket_id: UUID, db: Database, evaluation: Evaluation) -> dict:
    row = db.execute(evaluation_tickets(evaluation).where(Ticket.id == ticket_id)).one_or_none()
    if row is None:
        raise ApiError(404, "TICKET_EVALUATION_NOT_FOUND", "실습 티켓 평가를 찾을 수 없습니다.")
    entry, ticket, _ = row
    approved = set()
    links = []
    for ref in entry.evidence or []:
        try:
            if url := evidence_url(db, ref["kind"], UUID(ref["id"]), ticket, evaluation):
                approved.add(ref["id"])
                links.append({**ref, "source_url": url})
        except (TypeError, ValueError, KeyError):
            continue
    rules = None
    if evaluation.rules_status == "READY":
        rules = [{"rule_code": rule["rule_code"], "result": rule["result"], "evidence_refs": rule.get("evidence_refs", [])}
                 for rule in entry.rule_results or [] if set(rule.get("evidence_refs", [])) <= approved]
    feedback = entry.explanation if (evaluation.explanation_status == "READY"
                                     and len(approved) == len(entry.evidence or [])) else None
    frozen = next((item for item in evaluation.input_snapshot.get("tickets", []) if item["id"] == str(ticket.id)), {})
    timeline = [event for event in frozen.get("events", []) if event["type"] != "learning_evidence"
                or evidence_url(db, "learning", UUID(event["id"]), ticket, evaluation)]
    return {**ticket_summary(row, evaluation), "rule_results": rules, "feedback": feedback,
            "timeline": timeline, "warnings": frozen.get("warnings", []), "evidence": links}
