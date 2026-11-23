from collections import defaultdict
from datetime import datetime

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.handover.models import TicketWork
from app.handover.snapshot import work_view
from app.models import Ticket, TicketEvent
from app.simulation.models import LearningEvent, ScenarioTicketTemplate, SimulationRun, SimulationRunHost

ELIGIBLE = {"INITIAL", "CARRYOVER"}
RESOLVED = {"resolved", "closed"}
ATTEMPTS = {"virtual_action_performed", "work_created", "work_updated", "transitioned"}


def ticket_versions(db: Session, run: SimulationRun) -> dict[str, int]:
    return {str(ticket_id): version for ticket_id, version in db.execute(
        select(Ticket.id, Ticket.version).where(Ticket.simulation_run_id == run.id)
    )}


def frozen_input(db: Session, run: SimulationRun, now: datetime, items: list[dict],
                 open_items: dict, warnings: list[dict]) -> dict:
    rows = db.execute(select(Ticket, ScenarioTicketTemplate.template_key, SimulationRunHost.state).outerjoin(
        ScenarioTicketTemplate, ScenarioTicketTemplate.id == Ticket.scenario_ticket_template_id,
    ).join(SimulationRunHost, (SimulationRunHost.simulation_run_id == run.id)
           & (SimulationRunHost.scenario_host_id == Ticket.scenario_host_id)).where(
        Ticket.simulation_run_id == run.id,
    ).order_by(Ticket.number)).all()
    work = defaultdict(list)
    events = defaultdict(list)
    for task in db.scalars(select(TicketWork).join(Ticket, Ticket.id == TicketWork.ticket_id)
                           .where(Ticket.simulation_run_id == run.id).order_by(TicketWork.id)):
        work[task.ticket_id].append(work_view(task))
    for event in db.scalars(select(TicketEvent).where(
        TicketEvent.simulation_run_id == run.id, TicketEvent.event_sequence <= run.last_event_sequence,
    ).order_by(TicketEvent.event_sequence)):
        events[event.ticket_id].append({
            "id": event.id, "sequence": event.event_sequence, "occurred_at": event.created_at,
            "actor_id": event.actor_id, "type": event.event_type, "details": event.details,
        })
    for entry in db.scalars(select(LearningEvent).where(
        LearningEvent.simulation_run_id == run.id, LearningEvent.event_sequence <= run.last_event_sequence,
    )):
        events[entry.ticket_id].append({
            "id": entry.id, "sequence": entry.event_sequence, "occurred_at": entry.occurred_at,
            "actor_id": entry.actor_id, "type": "learning_evidence",
            "details": {"kind": entry.kind, "activity": entry.activity,
                        "source_id": entry.source_id, "action_event_id": entry.action_event_id},
        })
    selected = {item["id"] for item in items}
    tickets = []
    for ticket, key, state in rows:
        tid = str(ticket.id)
        timeline = sorted(events[ticket.id], key=lambda event: event["sequence"])
        tickets.append({
            "id": tid, "key": key, "origin": ticket.training_origin, "version": ticket.version,
            "status": ticket.status, "archived_at": ticket.archived_at, "asset_kind": state.get("asset_kind"),
            "title": ticket.title, "detected_at": ticket.detected_at, "resolution": ticket.resolution,
            "next_action": ticket.next_action, "impact": ticket.impact,
            "work": work[ticket.id], "events": timeline, "included": tid in selected,
            "handover_next_action": open_items.get(tid) or (ticket.next_action if tid in selected else None),
            "handover_note": open_items.get(tid),
            "warnings": [entry["code"] for entry in warnings if entry.get("ticket_id") == tid],
            "activity_status": "ATTEMPTED" if any(event["type"] in ATTEMPTS for event in timeline) else "NOT_ATTEMPTED",
        })
    return jsonable_encoder({"schema": 1, "last_event_sequence": run.last_event_sequence, "finalized_at": now,
                             "tickets": tickets, "warnings": warnings})
