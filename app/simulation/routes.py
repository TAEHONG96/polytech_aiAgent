from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select, text

from app.auth import CurrentUser, WriteUser
from app.db import Database
from app.http import ApiError
from app.knowledge.indexing import sync_ticket_index
from app.knowledge.models import SopDocument
from app.models import Ticket
from app.settings import settings
from app.simulation.access import OwnedRun, WritableRun, find_run
from app.simulation.models import Scenario, ScenarioHost, ScenarioShiftSlot, ScenarioTicketTemplate, SimulationRun, SimulationRunHost
from app.tickets import (Scope, Severity, TicketCreate, TicketStatus, TicketUpdate, Transition, archive, edit_ticket,
                         insert_ticket, record_event, select_ticket_page, ticket_by_id, transition_ticket, view)

router = APIRouter(tags=["simulation"])


class SwitchRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pause_active_run_id: UUID | None = None


class StartRun(SwitchRun):
    scenario_key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    scenario_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


StartKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$")]


def education_label(value: str) -> str:
    return "[교육용]" + value.removeprefix("[합성]") if value.startswith("[합성]") else value


def active_run(db: Database, user_id: UUID, *, lock: bool = False) -> SimulationRun | None:
    query = select(SimulationRun).where(
        SimulationRun.user_id == user_id, SimulationRun.status == "ACTIVE",
    )
    return db.scalar(query.with_for_update() if lock else query)


def lock_run_selection(db: Database, user_id: UUID) -> None:
    # Serialize starts and resumes without granting the runtime role UPDATE on users.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
               {"owner": f"simulation-start:{user_id}"})


def run_to_pause(db: Database, user_id: UUID, expected_id: UUID | None) -> SimulationRun | None:
    current = active_run(db, user_id, lock=True)
    current_id = current.id if current else None
    if current_id == expected_id:
        return current
    if expected_id is None:
        raise ApiError(409, "SIMULATION_ALREADY_ACTIVE", "진행 중인 실습이 있습니다.", {"run_id": str(current_id)})
    raise ApiError(409, "SIMULATION_ACTIVE_CHANGED", "진행 중인 실습이 변경되었습니다. 목록을 확인한 뒤 다시 선택하세요.",
                   {"run_id": str(current_id) if current_id else None})


def pause_run(db: Database, run: SimulationRun | None) -> None:
    if run is not None:
        run.status = "PAUSED"
        db.flush()


def schedule(db: Database, scenario: Scenario) -> list[dict]:
    slots = list(db.scalars(select(ScenarioShiftSlot).where(
        ScenarioShiftSlot.scenario_id == scenario.id,
    ).order_by(ScenarioShiftSlot.slot_order)))
    if (
        len(slots) != 3 or slots[0].ends_at != slots[1].starts_at
        or slots[1].ends_at != slots[2].starts_at or slots[1].starts_at != scenario.virtual_start_at
    ):
        raise ApiError(500, "INVALID_SCENARIO", "발행된 실습 근무 슬롯을 확인하세요.")
    tz = ZoneInfo(scenario.timezone)
    return [
        {"id": slot.id, "slot_order": slot.slot_order, "crew_code": slot.crew_code,
         "type": slot.shift_type, "actor_type": slot.actor_type,
         "starts_at": slot.starts_at.astimezone(tz), "ends_at": slot.ends_at.astimezone(tz)}
        for slot in slots
    ]


def run_view(db: Database, run: SimulationRun) -> dict:
    scenario = db.get(Scenario, run.scenario_id)
    return {
        "id": run.id, "status": run.status,
        "scenario": {"key": scenario.scenario_key, "version": scenario.version},
        "virtual_now": scenario.virtual_start_at.astimezone(ZoneInfo(scenario.timezone)),
        "started_at": run.started_at, "completed_at": run.completed_at,
        "shift": schedule(db, scenario)[1],
    }


def clone_seed(db: Database, run: SimulationRun, scenario: Scenario) -> None:
    templates = db.scalars(select(ScenarioTicketTemplate).where(
        ScenarioTicketTemplate.scenario_id == scenario.id,
    ).order_by(ScenarioTicketTemplate.template_key))
    tickets = {}
    observations = []
    historical_keys = set()
    for template in templates:
        fields = template.initial_ticket.copy()
        fields.pop("incident_key", None)
        fields.pop("sop_refs", None)
        history = fields.pop("observed_history", [])
        fields["description"] = education_label(fields["description"])
        for field in ("resolved_at", "recheck_at"):
            if fields.get(field) is not None:
                fields[field] = datetime.fromisoformat(fields[field])
        ticket = Ticket(
            **fields, id=uuid4(), detected_at=template.detected_at,
            simulation_run_id=run.id, scenario_id=scenario.id, scenario_host_id=template.scenario_host_id,
            scenario_ticket_template_id=template.id, training_origin=template.origin, created_by=run.user_id,
            version=1, created_at=run.started_at, updated_at=run.started_at,
        )
        tickets[template.template_key] = ticket
        if history:
            historical_keys.add(template.template_key)
            observations.append((template.detected_at, template.template_key, None))
            observations.extend((datetime.fromisoformat(entry["at"]), template.template_key, entry) for entry in history)
    report = scenario.config["incoming_report"]
    links = [
        {"ticket_id": str(tickets[key].id),
         "ticket_url": f"/api/v1/simulation-runs/{run.id}/tickets/{tickets[key].id}"}
        for key in report["ticket_templates"] if tickets[key].training_origin == "CARRYOVER"
    ]
    if len(links) != len(report["ticket_templates"]):
        raise ApiError(500, "INVALID_SCENARIO", "발행된 실습 인수 보고서를 확인하세요.")
    run.incoming_report_snapshot = {"summary": report["summary"], "tickets": links}
    db.add(run)
    db.flush()
    hosts = db.scalars(select(ScenarioHost).where(ScenarioHost.scenario_id == scenario.id))
    db.add_all([
        SimulationRunHost(simulation_run_id=run.id, scenario_id=scenario.id,
                          scenario_host_id=host.id, state=host.initial_state)
        for host in hosts
    ])
    db.flush()
    db.add_all(tickets.values())
    db.flush()
    for key, ticket in tickets.items():
        if key not in historical_keys:
            record_event(db, ticket, run.user_id, "scenario_seeded", {"origin": ticket.training_origin})
    for occurred_at, key, entry in sorted(observations, key=lambda observation: observation[0]):
        ticket = tickets[key]
        if entry is None:
            event = record_event(db, ticket, run.user_id, "scenario_seeded", {"origin": ticket.training_origin})
        else:
            ticket.version += 1
            event = record_event(db, ticket, run.user_id, "scenario_observation", {
                "reason": entry["observation"], "crew": entry["crew"],
            })
        event.created_at = occurred_at
    db.flush()
    for key in sorted(historical_keys):
        sync_ticket_index(db, tickets[key])


@router.get("/simulation")
def summary(db: Database, user: CurrentUser) -> dict:
    run = active_run(db, user.id)
    catalog = select(Scenario).where(Scenario.is_published.is_(True))
    if settings().database_url.database == "polytech_training_corpus":
        catalog = catalog.where(Scenario.scenario_key != "dco-basic")
    scenarios = db.scalars(catalog.order_by(Scenario.scenario_key, Scenario.version.desc()))
    return {
        "status": "ACTIVE" if run else "NOT_STARTED",
        "active_run_id": run.id if run else None,
        "available_scenarios": [{"scenario_key": row.scenario_key, "version": row.version, "title": education_label(row.title)}
                                for row in scenarios],
    }


@router.post("/simulation-runs", status_code=201)
def start(body: StartRun, key: StartKey, response: Response, db: Database, user: WriteUser) -> dict:
    lock_run_selection(db, user.id)
    previous = db.scalar(select(SimulationRun).where(
        SimulationRun.user_id == user.id, SimulationRun.start_request_key == key,
    ))
    if previous is not None:
        scenario = db.get(Scenario, previous.scenario_id)
        if (scenario.scenario_key, scenario.version, previous.start_paused_run_id) != (
            body.scenario_key, body.scenario_version, body.pause_active_run_id,
        ):
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "같은 키에 다른 실습 시작 정보를 사용할 수 없습니다.")
        response.status_code = 200
        return run_view(db, previous)

    current = run_to_pause(db, user.id, body.pause_active_run_id)
    scenario = db.scalar(select(Scenario).where(
        Scenario.scenario_key == body.scenario_key, Scenario.version == body.scenario_version,
        Scenario.is_published.is_(True),
    ))
    if scenario is None:
        raise ApiError(404, "SCENARIO_NOT_FOUND", "발행된 시나리오를 찾을 수 없습니다.")

    run = SimulationRun(id=uuid4(), user_id=user.id, scenario_id=scenario.id, status="ACTIVE", start_request_key=key,
                        start_paused_run_id=body.pause_active_run_id, started_at=datetime.now(timezone.utc),
                        incoming_report_snapshot={})
    result = run_view(db, run)
    pause_run(db, current)
    clone_seed(db, run, scenario)
    db.commit()
    response.headers["Location"] = f"/api/v1/simulation-runs/{run.id}"
    return result


@router.post("/simulation-runs/{run_id}/resume")
def resume(run_id: UUID, body: SwitchRun, db: Database, user: WriteUser) -> dict:
    lock_run_selection(db, user.id)
    run = find_run(db, run_id, user.id, lock=True)
    if run.status == "ACTIVE":
        return run_view(db, run)
    if run.status != "PAUSED":
        raise ApiError(409, "SIMULATION_COMPLETED", "완료된 회차는 실습 목록에서 새로 시작할 수 있습니다.")
    pause_run(db, run_to_pause(db, user.id, body.pause_active_run_id))
    run.status = "ACTIVE"
    result = run_view(db, run)
    db.commit()
    return result


@router.get("/simulation-runs/{run_id}")
def detail(db: Database, run: OwnedRun) -> dict:
    return run_view(db, run)


@router.get("/simulation-runs/{run_id}/schedule")
def read_schedule(db: Database, run: OwnedRun) -> dict:
    return {"slots": schedule(db, db.get(Scenario, run.scenario_id))}


@router.get("/simulation-runs/{run_id}/incoming-handover")
def incoming(run: OwnedRun) -> dict:
    return run.incoming_report_snapshot


@router.get("/simulation-runs/{run_id}/hosts")
def hosts(db: Database, run: OwnedRun) -> dict:
    rows = db.execute(select(ScenarioHost, SimulationRunHost.state)
                      .join(SimulationRunHost, SimulationRunHost.scenario_host_id == ScenarioHost.id)
                      .where(SimulationRunHost.simulation_run_id == run.id).order_by(ScenarioHost.host_key))
    return {"items": [
        {"id": host.id, "host_key": host.host_key, "hostname": host.hostname, "aliases": host.aliases, "state": state}
        for host, state in rows
    ]}


def training_ticket(ticket: Ticket) -> dict:
    data = view(ticket).model_dump()
    if ticket.scenario_ticket_template_id is not None:
        data["description"] = education_label(ticket.description)
    return {**data, "scenario_host_id": ticket.scenario_host_id,
            "training_origin": ticket.training_origin}


@router.get("/simulation-runs/{run_id}/tickets")
def tickets(db: Database, run: OwnedRun, q: Annotated[str | None, Query(max_length=200)] = None,
            status: TicketStatus | None = None, severity: Severity | None = None, scope: Scope = Scope.ACTIVE,
            limit: Annotated[int, Query(ge=1, le=100)] = 25,
            cursor: Annotated[str | None, Query(max_length=512)] = None) -> dict:
    rows, next_cursor = select_ticket_page(db, run.id, q, status, severity, scope, limit, cursor)
    return {"items": [training_ticket(ticket) for ticket in rows], "next_cursor": next_cursor}


@router.get("/simulation-runs/{run_id}/tickets/{ticket_id}")
def ticket_detail(ticket_id: UUID, db: Database, run: OwnedRun) -> dict:
    ticket = ticket_by_id(db, ticket_id, run_id=run.id)
    template = db.get(ScenarioTicketTemplate, ticket.scenario_ticket_template_id) if ticket.scenario_ticket_template_id else None
    result = training_ticket(ticket)
    if template is None or "sop_refs" not in template.initial_ticket:
        return result
    refs = [UUID(value) for value in template.initial_ticket["sop_refs"]]
    docs = db.scalars(select(SopDocument).where(
        SopDocument.id.in_(refs), SopDocument.training_approved.is_(True),
    ).order_by(SopDocument.title)).all() if refs else ()
    return {**result, "related_sops": [
        {"id": doc.id, "title": doc.title, "source_url": f"/api/v1/simulation-runs/{run.id}/sops/{doc.id}"}
        for doc in docs
    ]}


class TrainingTicketCreate(TicketCreate):
    scenario_host_id: UUID


@router.post("/simulation-runs/{run_id}/tickets", status_code=201)
def create_training_ticket(body: TrainingTicketCreate, response: Response, db: Database,
                           run: WritableRun, user: WriteUser) -> dict:
    if not db.scalar(select(SimulationRunHost.scenario_host_id).where(
        SimulationRunHost.simulation_run_id == run.id, SimulationRunHost.scenario_host_id == body.scenario_host_id,
    )):
        raise ApiError(404, "HOST_NOT_FOUND", "실습 호스트를 찾을 수 없습니다.")
    ticket = insert_ticket(db, user, body,
                           simulation_run_id=run.id, scenario_id=run.scenario_id,
                           scenario_host_id=body.scenario_host_id, training_origin="USER_CREATED")
    response.headers["Location"] = f"/api/v1/simulation-runs/{run.id}/tickets/{ticket.id}"
    return training_ticket(ticket)


@router.patch("/simulation-runs/{run_id}/tickets/{ticket_id}")
def update_training_ticket(ticket_id: UUID, body: TicketUpdate, db: Database, run: WritableRun, user: WriteUser) -> dict:
    return training_ticket(edit_ticket(db, user, ticket_id, body, run.id))


@router.post("/simulation-runs/{run_id}/tickets/{ticket_id}/transitions")
def transition_training_ticket(ticket_id: UUID, body: Transition, db: Database, run: WritableRun, user: WriteUser) -> dict:
    return training_ticket(transition_ticket(db, user, ticket_id, body, run.id))


@router.delete("/simulation-runs/{run_id}/tickets/{ticket_id}", status_code=204)
def archive_training_ticket(ticket_id: UUID, expected_version: Annotated[int, Query(ge=1)], db: Database,
                            run: WritableRun, user: WriteUser) -> None:
    archive(db, user, ticket_id, expected_version, run.id)


@router.get("/simulation-runs")
def run_history(db: Database, user: CurrentUser) -> dict:
    rows = db.scalars(select(SimulationRun).where(SimulationRun.user_id == user.id)
                     .order_by(SimulationRun.started_at.desc(), SimulationRun.id))
    return {"items": [run_view(db, run) for run in rows]}
