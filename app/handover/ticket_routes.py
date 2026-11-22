from uuid import UUID

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.auth import CurrentUser, WriteUser
from app.db import Database
from app.handover.models import ShiftMembership, TicketWork
from app.handover.schemas import TicketOperations, WorkInput
from app.handover.service import get_shift, utcnow
from app.handover.snapshot import work_view
from app.http import ApiError
from app.knowledge.indexing import sync_ticket_index
from app.models import Ticket, TicketEvent, User
from app.tickets import apply_changes, ensure_editable, record_event, ticket_by_id

router = APIRouter(prefix="/tickets", tags=["ticket operations"])
OPS_FIELDS = ("shift_instance_id", "owner_id", "warnings", "approval_required")


def require_owner(db: Database, owner_id: UUID | None) -> None:
    if owner_id and not db.scalar(select(User.id).where(User.id == owner_id, User.is_active.is_(True),
                                                        User.role == "operator")):
        raise ApiError(404, "OPERATOR_NOT_FOUND", "활성 운영자를 찾을 수 없습니다.")


def operations(db: Database, ticket: Ticket) -> dict:
    work = db.scalars(select(TicketWork).where(TicketWork.ticket_id == ticket.id).order_by(TicketWork.id))
    history = db.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
                         .order_by(TicketEvent.ticket_version.desc()))
    return {**{field: getattr(ticket, field) for field in OPS_FIELDS}, "version": ticket.version,
            "work": [work_view(task) for task in work],
            "history": [{"id": event.id, "version": event.ticket_version, "type": event.event_type,
                         "actor_id": event.actor_id, "details": event.details, "created_at": event.created_at}
                        for event in history]}


@router.get("/{ticket_id}/operations")
def read_operations(ticket_id: UUID, db: Database, user: CurrentUser) -> dict:
    return operations(db, ticket_by_id(db, ticket_id))


@router.put("/{ticket_id}/operations")
def update_operations(ticket_id: UUID, body: TicketOperations, db: Database, user: WriteUser) -> dict:
    return edit_operations(db, user, ticket_id, body)


def edit_operations(db: Database, user: User, ticket_id: UUID, body: TicketOperations, run_id: UUID | None = None) -> dict:
    ticket = ticket_by_id(db, ticket_id, lock=True, run_id=run_id)
    ensure_editable(ticket, body.expected_version)
    if run_id is not None:
        if body.shift_instance_id is not None or body.owner_id not in (None, user.id):
            raise ApiError(404, "ASSIGNMENT_NOT_FOUND", "실습 범위의 담당 또는 회차를 찾을 수 없습니다.")
        apply_changes(db, ticket, user.id, "operations_updated", body.model_dump(exclude={"expected_version"}))
        return operations(db, ticket)
    require_owner(db, body.owner_id)
    shift = get_shift(db, body.shift_instance_id) if body.shift_instance_id else None
    if body.owner_id and (shift is None or db.get(ShiftMembership, (shift.team_id, body.owner_id)) is None):
        raise ApiError(422, "INVALID_TICKET_OWNER", "담당자는 배정된 근무조의 소속 운영자여야 합니다.")
    apply_changes(db, ticket, user.id, "operations_updated", body.model_dump(exclude={"expected_version"}))
    return operations(db, ticket)


def save_work(db: Database, user: User, ticket_id: UUID, body: WorkInput, work_id: UUID | None = None,
              run_id: UUID | None = None) -> dict:
    ticket = ticket_by_id(db, ticket_id, lock=True, run_id=run_id)
    ensure_editable(ticket, body.expected_version)
    if run_id is None:
        require_owner(db, body.owner_id)
    elif body.owner_id not in (None, user.id):
        raise ApiError(404, "ASSIGNMENT_NOT_FOUND", "실습 범위의 담당자를 찾을 수 없습니다.")
    if ticket.status == "closed" and body.status != "done":
        raise ApiError(409, "TICKET_ALREADY_CLOSED", "종료된 티켓에는 미완료 작업을 기록할 수 없습니다.")
    task = db.scalar(select(TicketWork).where(TicketWork.id == work_id, TicketWork.ticket_id == ticket.id)) \
        if work_id else TicketWork(ticket_id=ticket.id)
    if task is None:
        raise ApiError(404, "WORK_NOT_FOUND", "이 티켓의 작업을 찾을 수 없습니다.")
    before = jsonable_encoder(work_view(task)) if work_id else None
    for field, value in body.model_dump(exclude={"expected_version"}).items():
        setattr(task, field, value)
    db.add(task)
    db.flush()
    after = jsonable_encoder(work_view(task))
    if before != after:
        ticket.version += 1
        ticket.updated_at = utcnow()
        record_event(db, ticket, user.id, "work_updated" if work_id else "work_created", {"before": before, "after": after})
        sync_ticket_index(db, ticket)
    db.commit()
    return operations(db, ticket)


@router.post("/{ticket_id}/work", status_code=201)
def add_work(ticket_id: UUID, body: WorkInput, db: Database, user: WriteUser) -> dict:
    return save_work(db, user, ticket_id, body)


@router.put("/{ticket_id}/work/{work_id}")
def update_work(ticket_id: UUID, work_id: UUID, body: WorkInput, db: Database, user: WriteUser) -> dict:
    return save_work(db, user, ticket_id, body, work_id)
