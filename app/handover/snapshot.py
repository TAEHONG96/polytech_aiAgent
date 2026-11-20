from datetime import datetime
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from app.handover.models import ShiftInstance, TicketWork
from app.handover.schemas import Selection
from app.http import ApiError
from app.models import Ticket, TicketEvent, User

ACTIVE = ("open", "in_progress", "waiting")


def work_view(work: TicketWork) -> dict:
    return {key: getattr(work, key) for key in ("id", "title", "status", "owner_id", "due_at", "note")}


def gaps(ticket: Ticket, work: list[TicketWork]) -> list[dict]:
    items = []

    def missing(key: str, field: str, question: str, recipient: UUID | None = None):
        items.append({"id": f"{ticket.id}:{key}", "field": field,
                      "question": f"{ticket.display_number} · {question}", "requested_from": recipient or ticket.owner_id})

    pending = [task for task in work if task.status != "done"]
    if ticket.status in ACTIVE or pending:
        checks = [
            ("impact", ticket.impact, "현재 영향 범위를 확인해 주세요."),
            ("owner", ticket.owner_id, "후속 조치 담당자를 지정하거나 확인해 주세요."),
            ("shift", ticket.shift_instance_id, "담당 근무 회차를 확인해 주세요."),
            ("followup", ticket.next_action, "다음 근무조가 수행할 조치를 확인해 주세요."),
            ("completed", ticket.resolution or any(task.status == "done" for task in work), "완료한 작업이 있는지 확인해 주세요."),
            ("pending", pending or ticket.next_action, "남아 있는 작업이 있는지 확인해 주세요."),
            ("warnings", ticket.warnings, "주의사항을 기록하거나 해당 없음으로 확인해 주세요."),
        ]
        if ticket.status == "waiting":
            checks.append(("recheck", ticket.recheck_at, "다시 확인할 시각을 지정해 주세요."))
        for field, present, question in checks:
            if not present:
                missing(field, field, question)
        if ticket.approval_required is None:
            missing("approval", "approval", "추가 승인이 필요한지 확인해 주세요.")
    for task in pending:
        if task.owner_id is None:
            missing(f"{task.id}:owner", "work_owner", f"‘{task.title}’ 작업의 담당자를 확인해 주세요.")
        if task.due_at is None:
            missing(f"{task.id}:due", "work_due", f"‘{task.title}’ 작업의 마감 시각을 확인해 주세요.", task.owner_id)
    return items


def collect_snapshot(
    db: Session, shift: ShiftInstance, selection: Selection, cutoff: datetime, *,
    validate_inclusions: set[UUID] | None = None,
) -> list[dict]:
    manual = selection.include_ticket_ids + selection.exclude_ticket_ids
    if manual:
        existing = set(db.scalars(select(Ticket.id).where(Ticket.id.in_(manual), Ticket.simulation_run_id.is_(None))))
        if missing := set(manual) - existing:
            raise ApiError(404, "TICKET_NOT_FOUND", "선택한 티켓을 찾을 수 없습니다.",
                           {"ticket_ids": sorted(map(str, missing))})
        if validate_inclusions and db.scalar(select(Ticket.id).where(
            Ticket.id.in_(validate_inclusions), Ticket.archived_at.is_not(None),
        ).limit(1)):
            raise ApiError(422, "TICKET_ARCHIVED", "보관된 티켓은 인수인계 대상에 추가할 수 없습니다.")

    assigned = aliased(ShiftInstance)
    within = and_(TicketEvent.created_at >= shift.starts_at, TicketEvent.created_at < shift.ends_at,
                  TicketEvent.created_at <= cutoff)
    activity = select(TicketEvent.id).where(TicketEvent.ticket_id == Ticket.id, within).exists()
    pending = select(TicketWork.id).where(TicketWork.ticket_id == Ticket.id, TicketWork.status != "done").exists()
    scheduled = and_(Ticket.status != "closed", Ticket.recheck_at.is_not(None))
    approval = and_(Ticket.status != "closed", Ticket.approval_required.is_(True))
    active = Ticket.status.in_(ACTIVE)
    carryover = assigned.ends_at <= shift.starts_at
    assigned_here = Ticket.shift_instance_id == shift.id
    scope = or_(assigned_here, Ticket.shift_instance_id.is_(None), carryover)
    automatic = and_(scope, or_(active, pending, scheduled, approval, activity))

    rows = db.execute(
        select(Ticket, active.label("active"), pending.label("pending"), scheduled.label("scheduled"),
               approval.label("approval"), activity.label("activity"), carryover.label("carryover"), assigned_here.label("assigned"))
        .outerjoin(assigned, Ticket.shift_instance_id == assigned.id)
        .where(Ticket.simulation_run_id.is_(None), Ticket.archived_at.is_(None), or_(automatic, Ticket.id.in_(selection.include_ticket_ids)),
               Ticket.id.not_in(selection.exclude_ticket_ids))
        .order_by(Ticket.severity, Ticket.number)
    ).all()

    ids = [row[0].id for row in rows]
    if not ids:
        return []
    events: dict[UUID, list] = {ticket_id: [] for ticket_id in ids}
    tasks: dict[UUID, list] = {ticket_id: [] for ticket_id in ids}
    for event in db.scalars(select(TicketEvent).where(TicketEvent.ticket_id.in_(ids))
                            .order_by(TicketEvent.ticket_version)):
        events[event.ticket_id].append({
            "id": event.id, "version": event.ticket_version, "type": event.event_type,
            "actor_id": event.actor_id, "details": event.details, "created_at": event.created_at,
        })
    for work in db.scalars(select(TicketWork).where(TicketWork.ticket_id.in_(ids)).order_by(TicketWork.id)):
        tasks[work.ticket_id].append(work)

    included = set(selection.include_ticket_ids)
    records = []
    for ticket, *flags in rows:
        reasons = [reason for reason, flag in zip(
            ("unresolved", "pending_work", "scheduled", "approval", "shift_activity", "carryover", "assigned"), flags,
        ) if flag]
        if ticket.id in included:
            reasons.append("manual")
        if ticket.shift_instance_id is None:
            reasons.append("unassigned")
        records.append({
            "id": ticket.id, "number": ticket.display_number, "reasons": reasons,
            **{field: getattr(ticket, field) for field in (
                "title", "service", "description", "severity", "status", "impact", "next_action", "recheck_at",
                "resolution", "shift_instance_id", "owner_id", "warnings", "approval_required", "updated_at", "version",
            )},
            "history": events[ticket.id], "work": [
                {**work_view(task), "overdue": task.status != "done" and task.due_at is not None and task.due_at < cutoff}
                for task in tasks[ticket.id]
            ],
            "gaps": gaps(ticket, tasks[ticket.id]),
        })
    owner_ids = {owner for item in records for owner in [item["owner_id"], *(w["owner_id"] for w in item["work"])] if owner}
    if owner_ids:
        names = dict(db.execute(select(User.id, User.display_name).where(User.id.in_(owner_ids))).all())
        for item in records:
            item["owner_name"] = names.get(item["owner_id"])
            for task in item["work"]:
                task["owner_name"] = names.get(task["owner_id"])
            for gap in item["gaps"]:
                gap["requested_from_name"] = names.get(gap["requested_from"])
    return jsonable_encoder(records)
