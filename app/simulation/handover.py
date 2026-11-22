from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.routes import queue_analysis, run_view
from app.agent.schemas import StrictInput
from app.db import Database
from app.handover.models import Handover, HandoverRevision
from app.handover.schemas import HandoverStatus, ItemNote, ReportBody, Selection
from app.handover.service import current_revision, record_revision, snapshot_items, utcnow, validate_body
from app.handover.snapshot import collect_snapshot
from app.http import ApiError
from app.simulation.access import OwnedRun, WritableRun
from app.simulation.models import ScenarioShiftSlot, SimulationRun
from app.tickets import Note

router = APIRouter(prefix="/simulation-runs/{run_id}/outgoing-handover", tags=["training handover"])


class DraftVersion(StrictInput):
    expected_revision: Annotated[int, Field(ge=0, strict=True)]


class OpenItem(StrictInput):
    ticket_id: UUID
    next_action: Note | None = None


class DraftContent(StrictInput):
    summary: Note | None = None
    open_items: Annotated[list[OpenItem], Field(max_length=100)] = Field(default_factory=list)


class DraftEdit(DraftVersion):
    content: DraftContent | None = None
    selection: Selection | None = None
    refresh_snapshot: bool = False


def report_for_run(db: Session, run: SimulationRun) -> Handover | None:
    report = db.scalar(select(Handover).where(Handover.simulation_run_id == run.id))
    if report is not None and report.created_by != run.user_id:
        raise ApiError(404, "HANDOVER_NOT_FOUND", "실습 인수인계 보고서를 찾을 수 없습니다.")
    return report


def simulation_slot(db: Session, scenario_id: UUID) -> ScenarioShiftSlot:
    return db.scalar(select(ScenarioShiftSlot).where(
        ScenarioShiftSlot.scenario_id == scenario_id, ScenarioShiftSlot.slot_order == 1,
    ))


def report_view(db: Session, run: SimulationRun, report: Handover, revision: HandoverRevision) -> dict:
    data = revision.content_snapshot
    items = snapshot_items(db, report.id, data["snapshot_version"], run_id=run.id)
    allowed_ids = {item["id"] for item in items}
    content = DraftContent.model_validate(data["content"])
    return {
        "run_id": run.id, "handover_id": report.id, "status": data["status"], "revision": revision.version,
        "content": {"summary": content.summary,
                    "open_items": [item for item in content.open_items if str(item.ticket_id) in allowed_ids]},
        "items": items,
        "warnings": data.get("warnings", []),
    }


@router.get("")
def read_draft(db: Database, run: OwnedRun) -> dict:
    report = report_for_run(db, run)
    if report is None:
        return {"run_id": run.id, "handover_id": None, "status": "DRAFT", "revision": 0,
                "content": DraftContent(), "items": [], "warnings": []}
    return report_view(db, run, report, current_revision(db, report))


@router.get("/revisions/{version}")
def revision_detail(version: int, db: Database, run: OwnedRun) -> dict:
    report = report_for_run(db, run)
    revision = db.get(HandoverRevision, (report.id, version)) if report else None
    if revision is None:
        raise ApiError(404, "HANDOVER_REVISION_NOT_FOUND", "실습 인계 리비전을 찾을 수 없습니다.")
    return report_view(db, run, report, revision)


def save_draft(db: Session, run: SimulationRun, edit: DraftEdit) -> Handover:
    report = report_for_run(db, run)
    current = report.version if report else 0
    if current != edit.expected_revision:
        raise ApiError(409, "VERSION_CONFLICT", "보고서가 변경되었습니다.", {"current_version": current})
    if report is not None and report.status != HandoverStatus.DRAFT:
        raise ApiError(409, "INVALID_HANDOVER_STATE", "현재 보고서는 초안을 수정할 수 없습니다.")
    previous = current_revision(db, report).content_snapshot if report else {}
    now = utcnow()
    slot = simulation_slot(db, run.scenario_id)
    selection = edit.selection or Selection.model_validate(previous.get("selection", {}))
    content = edit.content or DraftContent.model_validate(previous.get("content", {}))
    refresh = report is None or edit.refresh_snapshot or edit.selection is not None
    if report is None:
        report = Handover(simulation_run_id=run.id, status=HandoverStatus.DRAFT,
                          shift_start_at=slot.starts_at, shift_end_at=slot.ends_at, version=1, snapshot_version=1,
                          created_by=run.user_id, created_at=now, updated_at=now)
        db.add(report)
        db.flush()
    else:
        report.version += 1
    if refresh:
        items = collect_snapshot(db, slot, selection, now, run_id=run.id,
                                 validate_inclusions=set(selection.include_ticket_ids))
        report.snapshot_version = report.version
        if edit.content is None:
            retained = {item["id"] for item in items}
            content.open_items = [item for item in content.open_items if str(item.ticket_id) in retained]
    else:
        items = snapshot_items(db, report.id, report.snapshot_version, run_id=run.id)
    validate_body(ReportBody(summary=content.summary, item_notes=[
        ItemNote(ticket_id=item.ticket_id, followup=item.next_action) for item in content.open_items
    ]), items)
    record_revision(db, report, run.user_id, "refreshed" if refresh else "edited", {
        "content": content.model_dump(), "selection": selection.model_dump(), "captured_at": now,
    }, items if refresh else None)
    return report


@router.put("")
def edit_draft(body: DraftEdit, db: Database, run: WritableRun) -> dict:
    report = save_draft(db, run, body)
    db.commit()
    return report_view(db, run, report, current_revision(db, report))


@router.post("/generate", status_code=202)
def generate_draft(body: DraftVersion, response: Response, db: Database, run: WritableRun) -> dict:
    report = report_for_run(db, run)
    if report is None:
        report = save_draft(db, run, DraftEdit(expected_revision=body.expected_revision))
        db.flush()
    elif report.version != body.expected_revision:
        raise ApiError(409, "VERSION_CONFLICT", "보고서가 변경되었습니다.", {"current_version": report.version})
    elif report.status != HandoverStatus.DRAFT:
        raise ApiError(409, "INVALID_HANDOVER_STATE", "현재 보고서는 초안을 생성할 수 없습니다.")
    analysis = queue_analysis(db, run.user_id, "교육 인수인계 초안", None, run.id, handover_id=report.id,
                              handover_version=report.snapshot_version)
    response.headers["Location"] = f"/api/v1/simulation-runs/{run.id}/analyses/{analysis.id}"
    return {**run_view(analysis, []), "analysis_id": analysis.id}
