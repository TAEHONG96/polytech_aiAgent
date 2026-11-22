import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import Field, StringConstraints
from sqlalchemy import select

from app.agent.schemas import StrictInput
from app.db import Database
from app.handover.ticket_routes import operations
from app.http import ApiError
from app.knowledge.indexing import sync_ticket_index
from app.models import Ticket, TicketEvent
from app.simulation.access import OwnedRun, WritableRun
from app.simulation.models import Scenario, SimulationRunHost
from app.simulation.routes import StartKey
from app.tickets import Note, Version, ensure_editable, record_event, ticket_by_id

router = APIRouter(prefix="/simulation-runs/{run_id}", tags=["training activity"])
ACTION = "virtual_action_performed"
MEMO = "training_note"


class ActionInput(StrictInput):
    action_code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z_]{1,63}$")]
    parameters: Annotated[dict[str, str], Field(max_length=8)] = Field(default_factory=dict)
    note: Note | None = None
    expected_version: Version | None = None


def run_host(db: Database, ticket: Ticket) -> SimulationRunHost:
    host = db.get(SimulationRunHost, (ticket.simulation_run_id, ticket.scenario_host_id))
    if host is None:
        raise ApiError(404, "HOST_NOT_FOUND", "실습 호스트를 찾을 수 없습니다.")
    return host


def rules(db: Database, ticket: Ticket, host: SimulationRunHost) -> dict:
    catalog = db.get(Scenario, ticket.scenario_id).config.get("action_catalog", [])
    asset = host.state.get("asset_kind")
    return {item["code"]: (item["label"], item["variants"][asset]) for item in catalog if asset in item["variants"]}


def blocking_reason(state: dict, rule: dict) -> str | None:
    if any(state.get(field) != value for field, value in rule.get("requires", {}).items()):
        return rule["blocked"]
    return None


def receipt(event: TicketEvent) -> dict:
    actual = event.event_type == ACTION
    return {
        "action_id": event.id, "action_code": event.details["action_code"],
        "kind": "ACTION" if actual else "NOTE", "status": "COMPLETED" if actual else "RECORDED",
        "actor_id": event.actor_id, "occurred_at": event.created_at, "event_sequence": event.event_sequence,
        "ticket_version": event.ticket_version, "note": event.details["note"], "result": event.details["result"],
    }


@router.get("/tickets/{ticket_id}/actions")
def actions(ticket_id: UUID, db: Database, run: OwnedRun) -> dict:
    ticket = ticket_by_id(db, ticket_id, run_id=run.id)
    host = run_host(db, ticket)
    catalog = []
    for code, (label, rule) in rules(db, ticket, host).items():
        reason = blocking_reason(host.state, rule)
        catalog.append({"action_code": code, "label": label, "parameters": rule.get("parameter", {}),
                        "available": run.status == "ACTIVE" and ticket.archived_at is None and reason is None,
                        "blocked_reason": reason})
    events = db.scalars(select(TicketEvent).where(
        TicketEvent.simulation_run_id == run.id, TicketEvent.ticket_id == ticket.id,
        TicketEvent.event_type.in_((ACTION, MEMO)),
    ).order_by(TicketEvent.event_sequence))
    return {"items": operations(db, ticket)["work"], "catalog": catalog, "activity": [receipt(event) for event in events]}


@router.post("/tickets/{ticket_id}/actions", status_code=201)
def perform(ticket_id: UUID, body: ActionInput, key: StartKey, response: Response,
            db: Database, run: WritableRun) -> dict:
    ticket = ticket_by_id(db, ticket_id, lock=True, run_id=run.id)
    payload = json.dumps([str(ticket.id), body.model_dump()], sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    previous = db.scalar(select(TicketEvent).where(
        TicketEvent.simulation_run_id == run.id, TicketEvent.action_request_key == key,
    ))
    if previous is not None:
        if previous.action_request_digest != digest:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "같은 키에 다른 조치 요청을 사용할 수 없습니다.")
        response.status_code = 200
        return receipt(previous)

    ensure_editable(ticket, body.expected_version if body.expected_version is not None else ticket.version)
    parameters = body.parameters
    if body.action_code == "CUSTOM_NOTE":
        if not body.note or parameters:
            raise ApiError(422, "INVALID_ACTION_PARAMETERS", "자유 메모 내용만 입력하세요.")
        event_type, result = MEMO, None
    else:
        host = run_host(db, ticket)
        entry = rules(db, ticket, host).get(body.action_code)
        if entry is None:
            raise ApiError(422, "ACTION_NOT_AVAILABLE", "이 호스트의 시나리오 조치를 선택하세요.")
        _, rule = entry
        if parameters != rule.get("parameter", {}):
            raise ApiError(422, "INVALID_ACTION_PARAMETERS", "카탈로그의 조치 매개변수를 확인하세요.")
        if reason := blocking_reason(host.state, rule):
            raise ApiError(409, "ACTION_PRECONDITION_FAILED", reason)
        if changes := rule.get("set"):
            host.state = {**host.state, **changes}
        event_type = ACTION
        result = {"error_code": host.state.get("error_code"), "health": host.state.get("health"), "message": rule["message"]}

    ticket.version += 1
    ticket.updated_at = datetime.now(timezone.utc)
    event = record_event(db, ticket, run.user_id, event_type, {
        "action_code": body.action_code, "parameters": parameters, "note": body.note, "result": result,
    }, action_request_key=key, action_request_digest=digest)
    sync_ticket_index(db, ticket)
    db.flush()
    db.refresh(event)
    output = receipt(event)
    db.commit()
    return output


@router.get("/events")
def timeline(db: Database, run: OwnedRun, after_sequence: Annotated[int, Query(ge=0)] = 0,
             limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict:
    events = list(db.scalars(select(TicketEvent).where(
        TicketEvent.simulation_run_id == run.id, TicketEvent.event_sequence > after_sequence,
    ).order_by(TicketEvent.event_sequence).limit(limit + 1)))
    return {
        "items": [{"id": event.id, "ticket_id": event.ticket_id, "event_type": event.event_type,
                   "event_sequence": event.event_sequence, "occurred_at": event.created_at, "actor_id": event.actor_id,
                   "payload": event.details} for event in events[:limit]],
        "next_after_sequence": events[limit - 1].event_sequence if len(events) > limit else None,
    }
