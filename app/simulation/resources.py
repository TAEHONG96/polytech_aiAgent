from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from app.agent.models import AgentRun, AgentToolCall
from app.agent.routes import queue_analysis, run_view
from app.agent.schemas import MAX_MESSAGE
from app.auth import WriteUser
from app.db import Database
from app.handover.models import Handover
from app.handover.schemas import TicketOperations, WorkInput
from app.handover.ticket_routes import edit_operations, operations, save_work
from app.http import ApiError
from app.knowledge.models import SopDocument, SourceType
from app.knowledge.routes import SearchRequest, SearchResponse, SopDetail, SopPage, run_search, sop_detail, sop_original, sop_page
from app.knowledge.search import incident_scope
from app.models import Ticket
from app.simulation.access import OwnedRun, WritableRun
from app.simulation.models import ScenarioHost, ScenarioIncident
from app.tickets import ticket_by_id

router = APIRouter(prefix="/simulation-runs/{run_id}", tags=["training resources"])


class TrainingSearchRequest(SearchRequest):
    source_types: list[SourceType] = Field(default_factory=lambda: list(SourceType), min_length=1, max_length=3)
    ticket_id: UUID | None = None


class TrainingAnalyzeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE)]


@router.get("/tickets/{ticket_id}/operations")
def read_work(ticket_id: UUID, db: Database, run: OwnedRun) -> dict:
    return operations(db, ticket_by_id(db, ticket_id, run_id=run.id))


@router.put("/tickets/{ticket_id}/operations")
def update_training_operations(ticket_id: UUID, body: TicketOperations, db: Database,
                               run: WritableRun, user: WriteUser) -> dict:
    return edit_operations(db, user, ticket_id, body, run.id)


@router.post("/tickets/{ticket_id}/work", status_code=201)
def add_training_work(ticket_id: UUID, body: WorkInput, db: Database, run: WritableRun, user: WriteUser) -> dict:
    return save_work(db, user, ticket_id, body, run_id=run.id)


@router.put("/tickets/{ticket_id}/work/{work_id}")
def update_training_work(ticket_id: UUID, work_id: UUID, body: WorkInput, db: Database,
                         run: WritableRun, user: WriteUser) -> dict:
    return save_work(db, user, ticket_id, body, work_id, run_id=run.id)


@router.get("/sops")
def training_sops(db: Database, run: OwnedRun, limit: Annotated[int, Query(ge=1, le=100)] = 25,
                  cursor: UUID | None = None) -> SopPage:
    return sop_page(db, limit, cursor, run.id)


@router.get("/sops/{sop_id}")
def training_sop(sop_id: UUID, db: Database, run: OwnedRun) -> SopDetail:
    return sop_detail(db, sop_id, run.id)


@router.get("/sops/{sop_id}/original")
def training_original(sop_id: UUID, db: Database, run: OwnedRun):
    return sop_original(db, sop_id, approved=True)


@router.post("/knowledge/search")
def training_search(body: TrainingSearchRequest, db: Database, run: OwnedRun, user: WriteUser) -> SearchResponse:
    if body.ticket_id is not None:
        ticket_by_id(db, body.ticket_id, run_id=run.id)
    return run_search(db, body, run_id=run.id, target_id=body.ticket_id)


def incident_view(incident: ScenarioIncident) -> dict:
    return {"id": incident.id, "occurred_at": incident.occurred_at, "title": incident.title,
            "incident_id": incident.id, "incident_key": incident.incident_key, "error_code": incident.error_code,
            "source_type": "synthetic", "summary": incident.title, "resolution": incident.resolution}


@router.get("/tickets/{ticket_id}/host-incidents")
def host_incidents(ticket_id: UUID, db: Database, run: OwnedRun,
                   limit: Annotated[int, Query(ge=1, le=100)] = 20) -> dict:
    ticket = ticket_by_id(db, ticket_id, run_id=run.id)
    host = db.get(ScenarioHost, ticket.scenario_host_id) if ticket.scenario_host_id else None
    incidents = db.scalars(select(ScenarioIncident).where(
        *incident_scope(ticket), ScenarioIncident.scenario_host_id == ticket.scenario_host_id,
    ).order_by(ScenarioIncident.occurred_at.desc(), ScenarioIncident.id).limit(limit))
    prefix = f"/api/v1/simulation-runs/{run.id}/tickets/{ticket.id}/incident-sources"
    items = [{**incident_view(incident), "source_url": f"{prefix}/{incident.id}"} for incident in incidents]
    return {"host": {"id": host.id, "host_key": host.host_key, "hostname": host.hostname} if host else None,
            "incidents": items, "items": items}


@router.get("/tickets/{ticket_id}/incident-sources/{incident_id}")
def incident_source(ticket_id: UUID, incident_id: UUID, db: Database, run: OwnedRun) -> dict:
    ticket = ticket_by_id(db, ticket_id, run_id=run.id)
    incident = db.scalar(select(ScenarioIncident).where(ScenarioIncident.id == incident_id, *incident_scope(ticket)))
    if incident is None:
        raise ApiError(404, "INCIDENT_NOT_FOUND", "승인된 사고 근거를 찾을 수 없습니다.")
    return incident_view(incident)


@router.post("/tickets/{ticket_id}/analyses", status_code=202)
def analyze(ticket_id: UUID, body: TrainingAnalyzeInput, response: Response, db: Database, run: WritableRun) -> dict:
    ticket_by_id(db, ticket_id, run_id=run.id)
    analysis = queue_analysis(db, run.user_id, body.message, ticket_id, run.id)
    response.headers["Location"] = f"/api/v1/simulation-runs/{run.id}/analyses/{analysis.id}"
    return {**run_view(analysis, []), "analysis_id": analysis.id}


def evidence_allowed(db: Database, run_id: UUID, target: Ticket | None, item: dict) -> bool:
    try:
        source_id = UUID(str(item.get("source_id") or item["ticket_id"]))
    except (KeyError, ValueError):
        return False
    prefix = f"/api/v1/simulation-runs/{run_id}"
    kind = item.get("kind")
    if kind == "SOP":
        expected_url = f"{prefix}/sops/{source_id}"
        matches = db.scalar(select(SopDocument.id).where(
            SopDocument.id == source_id, SopDocument.training_approved.is_(True),
        ))
    elif kind == "HISTORICAL_INCIDENT" and target is not None:
        expected_url = f"{prefix}/tickets/{target.id}/incident-sources/{source_id}"
        matches = db.scalar(select(ScenarioIncident.id).where(
            ScenarioIncident.id == source_id, *incident_scope(target),
        ))
    elif kind in {"SIMILAR_TICKET", "OPEN_TICKET", "TICKET_DETAILS", "TICKET_HISTORY",
                 "ticket", "impact", "symptom", "completed", "followup", "warning", "gap", "history"}:
        expected_url = f"{prefix}/tickets/{source_id}"
        matches = db.scalar(select(Ticket.id).where(Ticket.id == source_id, Ticket.simulation_run_id == run_id))
    else:
        return False
    return matches is not None and item.get("source_url") == expected_url


@router.get("/analyses/{analysis_id}")
def analysis_detail(analysis_id: UUID, db: Database, run: OwnedRun) -> dict:
    analysis = db.scalar(select(AgentRun).where(
        AgentRun.id == analysis_id, AgentRun.simulation_run_id == run.id, AgentRun.requested_by == run.user_id,
    ))
    if analysis is None:
        raise ApiError(404, "AGENT_RUN_NOT_FOUND", "분석 실행을 찾을 수 없습니다.")
    target = ticket_by_id(db, analysis.ticket_id, run_id=run.id) if analysis.ticket_id else None
    if analysis.handover_id is not None:
        if not db.scalar(select(Handover.id).where(
            Handover.id == analysis.handover_id, Handover.simulation_run_id == run.id, Handover.created_by == run.user_id,
        )):
            raise ApiError(404, "AGENT_RUN_NOT_FOUND", "분석 실행을 찾을 수 없습니다.")
    if analysis.result:
        if analysis.handover_id:
            evidence = [item for key in ("highlights", "completed_work", "followups", "warnings", "needs_review", "references")
                        for item in analysis.result.get(key, [])]
            evidence.extend(item for group in analysis.result.get("possible_related_groups", []) for item in group)
        else:
            evidence = analysis.result.get("evidence", [])
        if not all(evidence_allowed(db, run.id, target, item) for item in evidence):
            raise ApiError(404, "EVIDENCE_NOT_FOUND", "승인된 분석 근거를 찾을 수 없습니다.")
    events = list(db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == analysis.id)
                            .order_by(AgentToolCall.sequence)))
    return {**run_view(analysis, events), "analysis_id": analysis.id}
