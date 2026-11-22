import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import Field, StrictBool, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.routes import queue_analysis, run_view
from app.agent.schemas import StrictInput
from app.db import Database
from app.handover.models import Handover, HandoverItem, HandoverRevision, TicketWork
from app.handover.schemas import HandoverStatus, ItemNote, ReportBody, Selection
from app.handover.service import current_revision, record_revision, snapshot_items, utcnow, validate_body
from app.handover.snapshot import collect_snapshot
from app.http import ApiError
from app.models import Ticket
from app.simulation.access import FinalizingRun, OwnedRun, WritableRun
from app.simulation.evaluation_input import ELIGIBLE, RESOLVED, frozen_input, ticket_versions
from app.simulation.models import Scenario, ScenarioShiftSlot, SimulationEvaluation, SimulationRun, SimulationTicketEvaluation
from app.simulation.routes import StartKey
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


class FinalizeInput(DraftVersion):
    confirm: StrictBool

    @field_validator("confirm")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("최종 확정을 확인하세요.")
        return value


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
        "run_id": run.id, "handover_id": report.id,
        "status": "FINALIZED" if revision.kind == "finalized" else data["status"], "revision": revision.version,
        "content": {"summary": content.summary,
                    "open_items": [item for item in content.open_items if str(item.ticket_id) in allowed_ids]},
        "items": items,
        "warnings": data.get("warnings", []),
        "finalized_at": revision.updated_at if revision.kind == "finalized" else None,
        "finalized_by": revision.updated_by if revision.kind == "finalized" else None,
    }


@router.get("")
def read_draft(db: Database, run: OwnedRun) -> dict:
    report = report_for_run(db, run)
    if report is None:
        return {"run_id": run.id, "handover_id": None, "status": "DRAFT", "revision": 0,
                "content": DraftContent(), "items": [], "warnings": []}
    view = report_view(db, run, report, current_revision(db, report))
    if report.status == HandoverStatus.DRAFT:
        view["warnings"] = collect_warnings(db, run, DraftContent.model_validate(view["content"]), view["items"])
    return view


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
        "source_versions": ticket_versions(db, run) if refresh else previous.get("source_versions"),
    }, items if refresh else None)
    return report


@router.put("")
def edit_draft(body: DraftEdit, db: Database, run: WritableRun) -> dict:
    save_draft(db, run, body)
    db.commit()
    return read_draft(db, run)


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


def collect_warnings(db: Session, run: SimulationRun, content: DraftContent, items: list[dict]) -> list[dict]:
    warnings = [] if content.summary else [{"code": "MISSING_SUMMARY"}]
    included = {item["id"] for item in items}
    next_actions = {str(item.ticket_id): item.next_action for item in content.open_items}
    for ticket in db.scalars(select(Ticket).where(Ticket.simulation_run_id == run.id).order_by(Ticket.number)):
        ticket_id = str(ticket.id)
        if ticket.status not in RESOLVED:
            if ticket_id not in included:
                warnings.append({"code": "UNRESOLVED_NOT_INCLUDED", "ticket_id": ticket_id})
            if not (next_actions.get(ticket_id) or (ticket_id in included and ticket.next_action)):
                warnings.append({"code": "MISSING_NEXT_ACTION", "ticket_id": ticket_id})
            if ticket.owner_id is None:
                warnings.append({"code": "MISSING_OWNER", "ticket_id": ticket_id})
            if ticket.status == "waiting" and ticket.recheck_at is None:
                warnings.append({"code": "MISSING_RECHECK", "ticket_id": ticket_id})
        if ticket_id in included and not ticket.impact:
            warnings.append({"code": "MISSING_IMPACT", "ticket_id": ticket_id})
    for task in db.scalars(select(TicketWork).join(Ticket, Ticket.id == TicketWork.ticket_id).where(
        Ticket.simulation_run_id == run.id, TicketWork.status != "done",
    ).order_by(TicketWork.id)):
        location = {"ticket_id": str(task.ticket_id), "work_id": str(task.id)}
        if str(task.ticket_id) not in included:
            warnings.append({"code": "PENDING_WORK_NOT_INCLUDED", **location})
        if task.owner_id is None:
            warnings.append({"code": "WORK_OWNER_MISSING", **location})
        if task.due_at is None:
            warnings.append({"code": "WORK_DUE_MISSING", **location})
    return warnings


def final_sources(db: Session, run: SimulationRun, report: Handover) -> tuple[dict, DraftContent, list[dict]]:
    revision = current_revision(db, report)
    if revision is None or report.status != HandoverStatus.DRAFT:
        raise ApiError(422, "INVALID_REPORT_ITEMS", "보고서 참조 형식을 확인하세요.")
    data = revision.content_snapshot
    count = len(list(db.scalars(select(HandoverItem.ticket_id).where(
        HandoverItem.handover_id == report.id, HandoverItem.snapshot_version == report.snapshot_version,
    ))))
    versions = ticket_versions(db, run)
    structurally_invalid = ApiError(422, "INVALID_REPORT_ITEMS", "보고서 참조 형식을 확인하세요.")
    try:
        items = snapshot_items(db, report.id, report.snapshot_version, run_id=run.id)
        content = DraftContent.model_validate(data["content"])
        selection = Selection.model_validate(data["selection"])
        if (count != len(items) or data["snapshot_version"] != report.snapshot_version
                or any(str(tid) not in versions for tid in selection.include_ticket_ids + selection.exclude_ticket_ids)):
            raise structurally_invalid
        validate_body(ReportBody(summary=content.summary, item_notes=[
            ItemNote(ticket_id=item.ticket_id, followup=item.next_action) for item in content.open_items
        ]), items)
        item_versions = {item["id"]: item["version"] for item in items}
        baseline = data.get("source_versions")
        if (baseline is not None and not isinstance(baseline, dict)) or any(
            type(version) is not int or version < 1 for version in (*item_versions.values(), *(baseline or {}).values())
        ):
            raise structurally_invalid
    except (ValidationError, KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        raise structurally_invalid from exc
    stale = versions != baseline if baseline is not None else any(versions.get(tid) != version for tid, version in item_versions.items())
    if stale:
        raise ApiError(409, "REPORT_SOURCE_CHANGED", "티켓 원본이 변경되었습니다. 자료를 다시 수집하세요.")
    return data, content, items


@router.post("/finalize")
def finalize_training(body: FinalizeInput, key: StartKey, db: Database, run: FinalizingRun) -> dict:
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    saved = db.scalar(select(SimulationEvaluation).where(SimulationEvaluation.simulation_run_id == run.id))
    if saved is not None and saved.finalize_key == key:
        if saved.finalize_digest != digest:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "같은 키에 다른 확정 요청을 사용할 수 없습니다.")
        return saved.finalize_response
    if run.status != "ACTIVE":
        raise ApiError(409, "VERSION_CONFLICT", "이미 확정된 보고서입니다.")
    report = report_for_run(db, run)
    if (report.version if report else 0) != body.expected_revision:
        raise ApiError(409, "VERSION_CONFLICT", "보고서가 변경되었습니다.")
    if report is None:
        report = save_draft(db, run, DraftEdit(expected_revision=0))
        db.flush()
    data, content, items = final_sources(db, run, report)
    warnings = collect_warnings(db, run, content, items)
    now = utcnow()
    snapshot = frozen_input(db, run, now, items, {str(item.ticket_id): item.next_action for item in content.open_items}, warnings)
    report.version += 1
    report.status = HandoverStatus.CONFIRMED
    report.confirmed_at, report.confirmed_by = now, run.user_id
    record_revision(db, report, run.user_id, "finalized", {**data, "warnings": warnings})
    db.flush()
    run.status, run.completed_at = "COMPLETED", now
    run.finalized_handover_id, run.finalized_revision = report.id, report.version
    result = {"run_id": str(run.id), "simulation_status": "COMPLETED", "report_status": "FINALIZED",
              "finalized_revision": report.version, "warnings": warnings,
              "evaluation": {"rules_status": "PENDING", "explanation_status": "NOT_STARTED"}}
    scenario = db.get(Scenario, run.scenario_id)
    evaluation = SimulationEvaluation(
        simulation_run_id=run.id, handover_id=report.id, handover_revision=report.version,
        rule_version=scenario.config["evaluation_rule_version"], input_snapshot=snapshot,
        rules_status="PENDING", explanation_status="NOT_STARTED", rules_available_at=now,
        created_at=now, updated_at=now, finalize_key=key, finalize_digest=digest, finalize_response=result,
    )
    db.add(evaluation)
    db.flush()
    db.add_all([SimulationTicketEvaluation(
        evaluation_id=evaluation.id, ticket_id=UUID(ticket["id"]), activity_status=ticket["activity_status"],
        final_status=ticket["status"],
    ) for ticket in snapshot["tickets"] if ticket["origin"] in ELIGIBLE])
    db.commit()
    return result
