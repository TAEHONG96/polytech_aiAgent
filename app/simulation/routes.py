from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select, text

from app.auth import CurrentUser, WriteUser
from app.db import Database
from app.http import ApiError
from app.models import Ticket
from app.simulation.models import Scenario, ScenarioHost, ScenarioShiftSlot, ScenarioTicketTemplate, SimulationRun, SimulationRunHost
from app.tickets import record_event, view

router = APIRouter(tags=["simulation"])


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    scenario_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


StartKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$")]


def active_run(db: Database, user_id: UUID) -> SimulationRun | None:
    return db.scalar(select(SimulationRun).where(
        SimulationRun.user_id == user_id, SimulationRun.status == "ACTIVE",
    ))


def owned_run(run_id: UUID, db: Database, user: CurrentUser) -> SimulationRun:
    run = db.scalar(select(SimulationRun).where(SimulationRun.id == run_id, SimulationRun.user_id == user.id))
    if run is None:
        raise ApiError(404, "SIMULATION_RUN_NOT_FOUND", "실습 실행을 찾을 수 없습니다.")
    return run


OwnedRun = Annotated[SimulationRun, Depends(owned_run)]


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
    for template in templates:
        ticket = Ticket(
            **template.initial_ticket, id=uuid4(), detected_at=template.detected_at,
            simulation_run_id=run.id, scenario_id=scenario.id, scenario_host_id=template.scenario_host_id,
            scenario_ticket_template_id=template.id, training_origin=template.origin, created_by=run.user_id,
            version=1, created_at=run.started_at, updated_at=run.started_at,
        )
        tickets[template.template_key] = ticket
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
    for ticket in tickets.values():
        record_event(db, ticket, run.user_id, "scenario_seeded", {"origin": ticket.training_origin})


@router.get("/simulation")
def summary(db: Database, user: CurrentUser) -> dict:
    run = active_run(db, user.id)
    scenarios = db.scalars(select(Scenario).where(Scenario.is_published.is_(True)).order_by(
        Scenario.scenario_key, Scenario.version,
    ))
    return {
        "status": "ACTIVE" if run else "NOT_STARTED",
        "active_run_id": run.id if run else None,
        "available_scenarios": [{"scenario_key": row.scenario_key, "version": row.version} for row in scenarios],
    }


@router.post("/simulation-runs", status_code=201)
def start(body: StartRun, key: StartKey, response: Response, db: Database, user: WriteUser) -> dict:
    # Serialize starts for one user without granting the runtime role UPDATE on users.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"),
               {"owner": f"simulation-start:{user.id}"})
    previous = db.scalar(select(SimulationRun).where(
        SimulationRun.user_id == user.id, SimulationRun.start_request_key == key,
    ))
    if previous is not None:
        scenario = db.get(Scenario, previous.scenario_id)
        if (scenario.scenario_key, scenario.version) != (body.scenario_key, body.scenario_version):
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "같은 키에 다른 시나리오를 요청할 수 없습니다.")
        response.status_code = 200
        return run_view(db, previous)

    current = active_run(db, user.id)
    if current is not None:
        raise ApiError(409, "SIMULATION_ALREADY_ACTIVE", "진행 중인 실습이 있습니다.", {"run_id": str(current.id)})
    scenario = db.scalar(select(Scenario).where(
        Scenario.scenario_key == body.scenario_key, Scenario.version == body.scenario_version,
        Scenario.is_published.is_(True),
    ))
    if scenario is None:
        raise ApiError(404, "SCENARIO_NOT_FOUND", "발행된 시나리오를 찾을 수 없습니다.")

    run = SimulationRun(id=uuid4(), user_id=user.id, scenario_id=scenario.id, status="ACTIVE", start_request_key=key,
                        started_at=datetime.now(timezone.utc), incoming_report_snapshot={})
    result = run_view(db, run)
    clone_seed(db, run, scenario)
    db.commit()
    response.headers["Location"] = f"/api/v1/simulation-runs/{run.id}"
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
    return {**view(ticket).model_dump(), "scenario_host_id": ticket.scenario_host_id,
            "training_origin": ticket.training_origin}


@router.get("/simulation-runs/{run_id}/tickets")
def tickets(db: Database, run: OwnedRun) -> dict:
    return {"items": [training_ticket(ticket) for ticket in db.scalars(
        select(Ticket).where(Ticket.simulation_run_id == run.id).order_by(Ticket.number),
    )]}


@router.get("/simulation-runs/{run_id}/tickets/{ticket_id}")
def ticket_detail(ticket_id: UUID, db: Database, run: OwnedRun) -> dict:
    ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id, Ticket.simulation_run_id == run.id))
    if ticket is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
    return training_ticket(ticket)
