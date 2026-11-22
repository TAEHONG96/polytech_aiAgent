from datetime import datetime, timezone
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from app.agent.models import AgentRun
from app.agent.schemas import RunStatus
from app.db import lock_ticket_snapshot
from app.handover.models import Handover, HandoverItem, HandoverRevision, ShiftInstance, ShiftMembership, ShiftTeam
from app.handover.schemas import HandoverCreate, HandoverEdit, HandoverStatus, ReportBody, Selection
from app.handover.snapshot import collect_snapshot
from app.http import ApiError
from app.models import Ticket, User


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_shift(db: Session, shift_id: UUID) -> ShiftInstance:
    shift = db.get(ShiftInstance, shift_id)
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", "근무 회차를 찾을 수 없습니다.")
    return shift


def next_shift(db: Session, shift: ShiftInstance, requested_id: UUID | None = None) -> ShiftInstance:
    if requested_id is not None:
        get_shift(db, requested_id)
    incoming = db.scalar(select(ShiftInstance).where(ShiftInstance.starts_at == shift.ends_at))
    if incoming is None or incoming.team_id == shift.team_id or (requested_id is not None and incoming.id != requested_id):
        raise ApiError(409, "INVALID_INCOMING_SHIFT", "인계조와 다른 조의 직후 근무 회차가 필요합니다.")
    return incoming


def shift_view(db: Session, shift: ShiftInstance) -> dict:
    return {
        "id": shift.id, "team_id": shift.team_id, "team_name": db.get(ShiftTeam, shift.team_id).name,
        "starts_at": shift.starts_at, "ends_at": shift.ends_at, "timezone": shift.timezone,
    }


def require_membership(db: Session, shift: ShiftInstance, actor_id: UUID, *, lead: bool = False) -> None:
    membership = db.get(ShiftMembership, (shift.team_id, actor_id))
    if membership is None or (lead and not membership.is_lead):
        raise ApiError(403, "HANDOVER_FORBIDDEN", "해당 근무조의 권한이 필요합니다.")


def get_report(db: Session, report_id: UUID, *, lock: bool = False) -> Handover:
    query = select(Handover).where(Handover.id == report_id, Handover.simulation_run_id.is_(None))
    report = db.scalar(query.with_for_update() if lock else query)
    if report is None:
        raise ApiError(404, "HANDOVER_NOT_FOUND", "인수인계 보고서를 찾을 수 없습니다.")
    return report


def require_version(report: Handover, version: int) -> None:
    if report.version != version:
        raise ApiError(409, "VERSION_CONFLICT", "보고서가 변경되었습니다.", {"current_version": report.version})


def require_status(report: Handover, *statuses: HandoverStatus) -> None:
    if report.status not in statuses:
        raise ApiError(409, "INVALID_HANDOVER_STATE", "현재 보고서 상태에서 수행할 수 없는 작업입니다.")


def latest_analysis(db: Session, report: Handover) -> AgentRun | None:
    return db.scalar(select(AgentRun).where(
        AgentRun.handover_id == report.id, AgentRun.handover_version == report.snapshot_version,
    ).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(1))


def require_analysis_idle(db: Session, report: Handover) -> None:
    active = db.scalar(select(AgentRun.id).where(
        AgentRun.handover_id == report.id, AgentRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
    ).limit(1))
    if active:
        raise ApiError(409, "HANDOVER_ANALYSIS_BUSY", "진행 중인 분석이 끝난 뒤 다시 시도하세요.", {"run_id": str(active)})


def snapshot_items(db: Session, report_id: UUID, snapshot_version: int, *, run_id: UUID | None = None) -> list[dict]:
    query = select(HandoverItem.ticket_snapshot).where(
        HandoverItem.handover_id == report_id, HandoverItem.snapshot_version == snapshot_version,
    )
    if run_id is not None:
        query = query.join(Ticket, HandoverItem.ticket_id == Ticket.id).where(
            Ticket.simulation_run_id == run_id,
            HandoverItem.ticket_snapshot["id"].astext == cast(Ticket.id, String),
        )
    items = list(db.scalars(query))
    return sorted(items, key=lambda item: (item["severity"], int(item["number"].split("-")[1])))


def current_revision(db: Session, report: Handover) -> HandoverRevision:
    return db.get(HandoverRevision, (report.id, report.version))


def record_revision(db: Session, report: Handover, actor_id: UUID, kind: str, content: dict,
                    items: list[dict] | None = None) -> None:
    report.updated_at = utcnow()
    content = {**content, "schema_version": 1, "status": report.status, "snapshot_version": report.snapshot_version}
    db.add(HandoverRevision(handover_id=report.id, version=report.version, content_snapshot=jsonable_encoder(content),
                            updated_by=actor_id, updated_at=report.updated_at, kind=kind))
    if items:
        db.flush()
        db.add_all(HandoverItem(handover_id=report.id, snapshot_version=report.snapshot_version,
                                ticket_id=UUID(item["id"]), ticket_snapshot=item) for item in items)


def create_report(db: Session, body: HandoverCreate, user: User, parent: Handover | None = None) -> Handover:
    outgoing = get_shift(db, body.outgoing_shift_id)
    require_membership(db, outgoing, user.id)
    incoming = next_shift(db, outgoing, body.incoming_shift_id)
    now = utcnow()
    if outgoing.starts_at > now:
        raise ApiError(409, "SHIFT_NOT_STARTED", "시작한 근무 회차의 인수인계를 작성하세요.")
    query = select(Handover.id).where(Handover.outgoing_shift_id == outgoing.id, Handover.supersedes_id.is_(None))
    if parent:
        query = select(Handover.id).where(Handover.supersedes_id == parent.id)
    if existing := db.scalar(query):
        raise ApiError(409, "HANDOVER_EXISTS", "이미 작성된 보고서가 있습니다.", {"handover_id": str(existing)})

    selection = Selection(**body.model_dump(include={"include_ticket_ids", "exclude_ticket_ids"}))
    items = collect_snapshot(db, outgoing, selection, now,
                             validate_inclusions=set(selection.include_ticket_ids) if parent is None else None)
    report_body = ReportBody()
    if parent:
        previous_body = ReportBody.model_validate(current_revision(db, parent).content_snapshot["body"])
        report_body = retain_body(previous_body, snapshot_items(db, parent.id, parent.snapshot_version), items)
    report = Handover(
        outgoing_shift_id=outgoing.id, incoming_shift_id=incoming.id, supersedes_id=parent.id if parent else None,
        shift_start_at=outgoing.starts_at, shift_end_at=outgoing.ends_at, status=HandoverStatus.DRAFT,
        version=1, snapshot_version=1, created_by=user.id, created_at=now, updated_at=now,
    )
    db.add(report)
    db.flush()
    record_revision(db, report, user.id, "created", {
        "body": report_body.model_dump(), "selection": selection.model_dump(), "captured_at": now,
        "outgoing_shift": shift_view(db, outgoing), "incoming_shift": shift_view(db, incoming),
    }, items)
    db.commit()
    return report


def validate_body(body: ReportBody, items: list[dict], *, ready: bool = False) -> None:
    gaps = {gap["id"] for item in items for gap in item["gaps"]}
    responses = {response.gap_id for response in body.gap_responses}
    notes = {str(note.ticket_id) for note in body.item_notes}
    if (not responses <= gaps or len(responses) != len(body.gap_responses)
            or not notes <= {item["id"] for item in items} or len(notes) != len(body.item_notes)):
        raise ApiError(422, "INVALID_REPORT_ITEMS", "현재 스냅샷의 티켓과 확인 항목을 한 번씩 지정하세요.")
    if ready and (not body.summary or gaps - responses):
        raise ApiError(422, "REPORT_INCOMPLETE", "인계 요약과 모든 누락 항목의 답변 또는 미확인 사유가 필요합니다.",
                       {"gap_ids": sorted(gaps - responses)})


def retain_body(body: ReportBody, old_items: list[dict], items: list[dict]) -> ReportBody:
    old_by_id = {item["id"]: item for item in old_items}
    available = {item["id"] for item in items}
    same = {gap["id"] for item in items if item == old_by_id.get(item["id"]) for gap in item["gaps"]}
    return body.model_copy(update={
        "gap_responses": [response for response in body.gap_responses if response.gap_id in same],
        "item_notes": [note for note in body.item_notes if str(note.ticket_id) in available],
    })


def edit_report(db: Session, report_id: UUID, edit: HandoverEdit, user: User) -> Handover:
    lock_ticket_snapshot(db)
    report = get_report(db, report_id, lock=True)
    outgoing = get_shift(db, report.outgoing_shift_id)
    require_membership(db, outgoing, user.id)
    require_version(report, edit.expected_version)
    require_status(report, HandoverStatus.DRAFT, HandoverStatus.READY)
    content = current_revision(db, report).content_snapshot.copy()
    old_items = snapshot_items(db, report.id, report.snapshot_version)
    items = old_items
    body = edit.body or ReportBody.model_validate(content["body"])
    report.version += 1
    refreshed = edit.refresh_snapshot or edit.selection is not None
    if refreshed:
        require_analysis_idle(db, report)
        now = utcnow()
        previous = Selection.model_validate(content["selection"])
        selection = edit.selection or previous
        items = collect_snapshot(db, outgoing, selection, now,
                                 validate_inclusions=set(selection.include_ticket_ids) - set(previous.include_ticket_ids))
        validate_body(body, old_items)
        body = retain_body(body, old_items, items)
        report.snapshot_version = report.version
        content.update(selection=selection.model_dump(), captured_at=now)
    if edit.status == HandoverStatus.READY:
        require_analysis_idle(db, report)
    validate_body(body, items, ready=edit.status == HandoverStatus.READY)
    report.status = edit.status
    content["body"] = body.model_dump()
    record_revision(db, report, user.id, "refreshed" if refreshed else "edited", content, items if refreshed else None)
    db.commit()
    return report


def finalize(db: Session, report_id: UUID, version: int, user: User, *, acknowledge: bool = False) -> Handover:
    lock_ticket_snapshot(db)
    report = get_report(db, report_id, lock=True)
    shift = get_shift(db, report.incoming_shift_id if acknowledge else report.outgoing_shift_id)
    require_membership(db, shift, user.id, lead=not acknowledge)
    require_version(report, version)
    require_status(report, HandoverStatus.CONFIRMED if acknowledge else HandoverStatus.READY)
    content = current_revision(db, report).content_snapshot.copy()
    now = utcnow()
    if acknowledge:
        report.status = HandoverStatus.ACKNOWLEDGED
        report.acknowledged_by, report.acknowledged_at = user.id, now
    else:
        require_analysis_idle(db, report)
        items = snapshot_items(db, report.id, report.snapshot_version)
        if items != collect_snapshot(db, shift, Selection.model_validate(content["selection"]), now):
            raise ApiError(409, "HANDOVER_STALE", "원본 티켓이나 작업이 변경되었습니다. 자료를 새로 수집하세요.")
        validate_body(ReportBody.model_validate(content["body"]), items, ready=True)
        report.status = HandoverStatus.CONFIRMED
        report.confirmed_by, report.confirmed_at = user.id, now
    report.version += 1
    record_revision(db, report, user.id, "acknowledged" if acknowledge else "confirmed", content)
    db.commit()
    return report
