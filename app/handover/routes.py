from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError

from app.agent.models import AgentRun, AgentToolCall
from app.agent.routes import run_view
from app.agent.schemas import RunStatus
from app.auth import CurrentUser, WriteUser
from app.db import Database, lock_ticket_snapshot
from app.handover.models import Handover, HandoverRevision, ShiftInstance, ShiftMembership
from app.handover.schemas import HandoverCreate, HandoverEdit, HandoverStatus, Selection, VersionInput
from app.handover.service import (
    create_report, current_revision, edit_report, finalize, get_report, get_shift, latest_analysis, next_shift,
    require_analysis_idle, require_membership, require_status, require_version, shift_view, snapshot_items, utcnow,
)
from app.handover.snapshot import ACTIVE, collect_snapshot
from app.http import ApiError
from app.models import User

router = APIRouter(tags=["handovers"])
ACTIVE_REPORTS = (HandoverStatus.DRAFT, HandoverStatus.READY)


@router.get("/shift-instances")
def list_shifts(db: Database, user: CurrentUser) -> dict:
    shifts = db.scalars(select(ShiftInstance).order_by(ShiftInstance.starts_at.desc()).limit(100)).all()
    memberships = {item.team_id: item for item in db.scalars(
        select(ShiftMembership).where(ShiftMembership.user_id == user.id)
    )}
    return {"items": [{
        **shift_view(db, shift), "is_member": shift.team_id in memberships,
        "is_lead": shift.team_id in memberships and memberships[shift.team_id].is_lead,
    } for shift in shifts]}


@router.get("/shift-instances/{shift_id}/handover-preview")
def preview(shift_id: UUID, db: Database, user: CurrentUser) -> dict:
    lock_ticket_snapshot(db)
    outgoing = get_shift(db, shift_id)
    incoming = next_shift(db, outgoing)
    now = utcnow()
    if outgoing.starts_at > now:
        raise ApiError(409, "SHIFT_NOT_STARTED", "근무가 시작되면 인수인계 대상을 조회할 수 있습니다.")
    return {"outgoing_shift": shift_view(db, outgoing), "incoming_shift": shift_view(db, incoming),
            "captured_at": now, "items": collect_snapshot(db, outgoing, Selection(), now)}


@router.get("/handover-operators")
def operators(db: Database, user: CurrentUser) -> dict:
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.display_name, User.id))
    memberships: dict[UUID, list] = {}
    for member in db.scalars(select(ShiftMembership)):
        memberships.setdefault(member.user_id, []).append(member.team_id)
    return {"items": [{"id": item.id, "name": item.display_name, "team_ids": memberships.get(item.id, [])} for item in users]}


def report_summary(db: Database, report: Handover) -> dict:
    return {
        **{field: getattr(report, field) for field in (
            "id", "status", "version", "snapshot_version", "supersedes_id", "created_at", "updated_at",
            "confirmed_at", "acknowledged_at", "created_by", "confirmed_by", "acknowledged_by",
            "shift_start_at", "shift_end_at", "outgoing_shift_id", "incoming_shift_id",
        )},
        "outgoing_shift": shift_view(db, get_shift(db, report.outgoing_shift_id)),
        "incoming_shift": shift_view(db, get_shift(db, report.incoming_shift_id)),
    }


@router.get("/handovers")
def list_reports(db: Database, user: CurrentUser, status: HandoverStatus | None = None,
                 outgoing_shift_id: UUID | None = None, before: UUID | None = None,
                 limit: Annotated[int, Query(ge=1, le=100)] = 25) -> dict:
    query = select(Handover)
    if status:
        query = query.where(Handover.status == status)
    if outgoing_shift_id:
        query = query.where(Handover.outgoing_shift_id == outgoing_shift_id)
    if before:
        point = get_report(db, before)
        query = query.where(tuple_(Handover.created_at, Handover.id) < (point.created_at, point.id))
    rows = list(db.scalars(query.order_by(Handover.created_at.desc(), Handover.id.desc()).limit(limit + 1)))
    return {"items": [report_summary(db, report) for report in rows[:limit]],
            "next_before": rows[limit - 1].id if len(rows) > limit else None}


def report_detail(db: Database, report: Handover, user: User) -> dict:
    revisions = list(db.scalars(select(HandoverRevision).where(
        HandoverRevision.handover_id == report.id, HandoverRevision.version <= report.version)
                                .order_by(HandoverRevision.version.desc())))
    users = dict(db.execute(select(User.id, User.display_name).where(User.id.in_({
        entry.updated_by for entry in revisions
    }))).all())
    content = revisions[0].content_snapshot
    items = snapshot_items(db, report.id, report.snapshot_version)
    outgoing = db.get(ShiftMembership, (get_shift(db, report.outgoing_shift_id).team_id, user.id))
    incoming = db.get(ShiftMembership, (get_shift(db, report.incoming_shift_id).team_id, user.id))
    run = latest_analysis(db, report)
    calls = list(db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id)
                            .order_by(AgentToolCall.sequence))) if run else []
    child = db.scalar(select(Handover.id).where(Handover.supersedes_id == report.id))
    return {
        **report_summary(db, report), **content, "items": items, "superseded_by": child,
        "counts": {"total": len(items), "unresolved": sum(item["status"] in ACTIVE for item in items),
                   "completed_work": sum(work["status"] == "done" for item in items for work in item["work"])},
        "analysis": run_view(run, calls) if run else None,
        "revisions": [{"version": entry.version, "kind": entry.kind, "updated_at": entry.updated_at,
                       "updated_by": entry.updated_by, "updated_by_name": users.get(entry.updated_by)} for entry in revisions],
        "permissions": {
            "edit": bool(outgoing) and report.status in ACTIVE_REPORTS,
            "confirm": bool(outgoing and outgoing.is_lead) and report.status == HandoverStatus.READY,
            "acknowledge": bool(incoming) and report.status == HandoverStatus.CONFIRMED,
            "revise": bool(outgoing) and report.status not in ACTIVE_REPORTS and child is None,
        },
    }


@router.post("/handovers", status_code=201)
def create(body: HandoverCreate, response: Response, db: Database, user: WriteUser) -> dict:
    lock_ticket_snapshot(db)
    report = create_report(db, body, user)
    response.headers["Location"] = f"/api/v1/handovers/{report.id}"
    return report_detail(db, report, user)


@router.get("/handovers/{report_id}")
def read(report_id: UUID, db: Database, user: CurrentUser) -> dict:
    report = get_report(db, report_id)
    return report_detail(db, report, user)


@router.get("/handovers/{report_id}/revisions/{version}")
def read_revision(report_id: UUID, version: int, db: Database, user: CurrentUser) -> dict:
    revision = db.get(HandoverRevision, (report_id, version))
    if revision is None:
        raise ApiError(404, "REVISION_NOT_FOUND", "보고서 수정 이력을 찾을 수 없습니다.")
    content = revision.content_snapshot
    return {"version": version, **content, "updated_at": revision.updated_at, "updated_by": revision.updated_by,
            "items": snapshot_items(db, report_id, content["snapshot_version"])}


@router.put("/handovers/{report_id}")
def update(report_id: UUID, body: HandoverEdit, db: Database, user: WriteUser) -> dict:
    return report_detail(db, edit_report(db, report_id, body, user), user)


@router.post("/handovers/{report_id}/confirm")
def confirm(report_id: UUID, body: VersionInput, db: Database, user: WriteUser) -> dict:
    return report_detail(db, finalize(db, report_id, body.expected_version, user), user)


@router.post("/handovers/{report_id}/acknowledge")
def acknowledge(report_id: UUID, body: VersionInput, db: Database, user: WriteUser) -> dict:
    return report_detail(db, finalize(db, report_id, body.expected_version, user, acknowledge=True), user)


@router.post("/handovers/{report_id}/revisions", status_code=201)
def revise(report_id: UUID, body: VersionInput, response: Response, db: Database, user: WriteUser) -> dict:
    lock_ticket_snapshot(db)
    parent = get_report(db, report_id, lock=True)
    require_membership(db, get_shift(db, parent.outgoing_shift_id), user.id)
    require_version(parent, body.expected_version)
    require_status(parent, HandoverStatus.CONFIRMED, HandoverStatus.ACKNOWLEDGED)
    selection = current_revision(db, parent).content_snapshot["selection"]
    report = create_report(db, HandoverCreate(outgoing_shift_id=parent.outgoing_shift_id, **selection), user, parent)
    response.headers["Location"] = f"/api/v1/handovers/{report.id}"
    return report_detail(db, report, user)


@router.post("/handovers/{report_id}/analyze", status_code=202)
def analyze(report_id: UUID, body: VersionInput, response: Response, db: Database, user: WriteUser) -> dict:
    report = get_report(db, report_id, lock=True)
    require_membership(db, get_shift(db, report.outgoing_shift_id), user.id)
    require_version(report, body.expected_version)
    require_status(report, HandoverStatus.DRAFT)
    require_analysis_idle(db, report)
    user_id = user.id
    run = AgentRun(requested_by=user_id, handover_id=report.id, handover_version=report.snapshot_version,
                   message="교대 인수인계 초안", status=RunStatus.QUEUED, created_at=utcnow())
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if name not in {"uq_agent_runs_active_user", "uq_agent_runs_active_handover"}:
            raise
        raise ApiError(409, "AGENT_BUSY", "진행 중인 분석이 끝난 뒤 다시 시도하세요.") from exc
    response.headers["Location"] = f"/api/v1/handovers/{report_id}"
    return run_view(run, [])
