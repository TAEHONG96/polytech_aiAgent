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
from app.simulation.corpus_actions import SCENARIOS as CORPUS_SCENARIOS, options as corpus_options
from app.simulation.learning import learning_source
from app.simulation.models import LearningEvent, Scenario, SimulationRunHost
from app.simulation.network_actions import NETWORK_SCENARIO, NETWORK_VERSION, network_options
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
    scenario = db.get(Scenario, ticket.scenario_id)
    catalog = scenario.config.get("action_catalog", [])
    asset = host.state.get("asset_kind")
    result = {item["code"]: (item["label"], item["variants"][asset]) for item in catalog if asset in item["variants"]}
    network_pilot = (scenario.scenario_key, scenario.version) == (NETWORK_SCENARIO, NETWORK_VERSION)
    if scenario.scenario_key in CORPUS_SCENARIOS and (scenario.version == "1.0" or network_pilot):
        result = {code: (label, {**rule, "repeatable": code != "REPLACE_MEMORY"}) for code, (label, rule) in result.items()}
        if scenario.scenario_key in {"synthetic-ops-major", "synthetic-ops-recovery"} and asset != "incident":
            result.update(corpus_options({**host.state, "asset_kind": "incident"}))
        result.update(corpus_options(host.state))
        if network_pilot and "network_observations" in host.state:
            result.update(network_options(host.state))
    return result


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
    events = db.scalars(select(TicketEvent).where(
        TicketEvent.simulation_run_id == run.id, TicketEvent.ticket_id == ticket.id,
        TicketEvent.event_type.in_((ACTION, MEMO)),
    ).order_by(TicketEvent.event_sequence)).all()
    completed = {event.details["action_code"] for event in events if event.event_type == ACTION}
    catalog = []
    for code, (label, rule) in rules(db, ticket, host).items():
        reason = ("이미 수행한 조치입니다." if rule.get("once") and code in completed else blocking_reason(host.state, rule))
        catalog.append({"action_code": code, "label": label, "parameters": rule.get("parameter", {}),
                        "available": run.status == "ACTIVE" and ticket.archived_at is None and reason is None,
                        "blocked_reason": reason, "repeatable": rule.get("repeatable", False),
                        "requires_note": rule.get("requires_note", False)})
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
        if rule.get("requires_note") and not body.note:
            raise ApiError(422, "ACTION_NOTE_REQUIRED", "확인한 사실이나 인계할 위험을 입력하세요.")
        if rule.get("once") and db.scalar(select(TicketEvent.id).where(
            TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == ACTION,
            TicketEvent.details["action_code"].astext == body.action_code,
        ).limit(1)) is not None:
            raise ApiError(409, "ACTION_ALREADY_PERFORMED", "이미 수행한 조치입니다.")
        if reason := blocking_reason(host.state, rule):
            raise ApiError(409, "ACTION_PRECONDITION_FAILED", reason)
        if changes := rule.get("set"):
            host.state = {**host.state, **changes}
        event_type = ACTION
        result = {"error_code": host.state.get("error_code"), "health": host.state.get("health"), "message": rule["message"]}
        result.update({field: rule[field] for field in ("outcome", "observations", "unconfirmed") if field in rule})

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
    page = [{"id": event.id, "ticket_id": event.ticket_id, "event_type": event.event_type,
             "event_sequence": event.event_sequence, "occurred_at": event.created_at, "actor_id": event.actor_id,
             "payload": event.details} for event in events]
    learning = db.execute(select(LearningEvent, Ticket).join(Ticket, Ticket.id == LearningEvent.ticket_id).where(
        LearningEvent.simulation_run_id == run.id, Ticket.simulation_run_id == run.id,
        LearningEvent.event_sequence > after_sequence,
    ).order_by(LearningEvent.event_sequence).limit(limit + 1)).all()
    page += [{"id": item.id, "ticket_id": item.ticket_id, "event_type": "learning_evidence",
              "event_sequence": item.event_sequence, "occurred_at": item.occurred_at, "actor_id": item.actor_id,
              "payload": {"kind": item.kind, "activity": item.activity, "source_id": item.source_id}
              if learning_source(db, ticket, item.kind, item.source_id) else None}
             for item, ticket in learning]
    page.sort(key=lambda item: item["event_sequence"])
    return {"items": [item for item in page[:limit] if item["payload"] is not None],
            "next_after_sequence": page[limit - 1]["event_sequence"] if len(page) > limit else None}
