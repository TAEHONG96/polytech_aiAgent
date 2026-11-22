import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi.encoders import jsonable_encoder
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session

from app.auth import CurrentUser, WriteUser
from app.db import Database, lock_ticket_snapshot
from app.handover.models import TicketWork
from app.http import ApiError
from app.knowledge.indexing import sync_ticket_index
from app.models import Ticket, TicketEvent, User

router = APIRouter(prefix="/tickets", tags=["tickets"])


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class Scope(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    ALL = "all"


TRANSITIONS = {
    TicketStatus.OPEN: [TicketStatus.IN_PROGRESS, TicketStatus.WAITING, TicketStatus.RESOLVED],
    TicketStatus.IN_PROGRESS: [TicketStatus.WAITING, TicketStatus.RESOLVED],
    TicketStatus.WAITING: [TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED],
    TicketStatus.RESOLVED: [TicketStatus.CLOSED, TicketStatus.IN_PROGRESS],
    TicketStatus.CLOSED: [TicketStatus.IN_PROGRESS],
}

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
Service = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Version = Annotated[int, Field(ge=1, strict=True)]
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|z|[+-]\d{2}:\d{2})")


def require_timestamp(value: object) -> str:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        raise ValueError("시간과 UTC 오프셋을 RFC 3339 형식으로 입력하세요.")
    return value


InputTime = Annotated[
    AwareDatetime,
    BeforeValidator(require_timestamp),
    AfterValidator(lambda value: value.astimezone(timezone.utc)),
]


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    description: Description
    service: Service
    severity: Severity
    detected_at: InputTime
    impact: Note | None = None
    next_action: Note | None = None


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Version
    title: Title | None = None
    description: Description | None = None
    service: Service | None = None
    severity: Severity | None = None
    detected_at: InputTime | None = None
    impact: Note | None = None
    next_action: Note | None = None
    recheck_at: InputTime | None = None

    @model_validator(mode="after")
    def validate_changes(self):
        provided = self.model_fields_set - {"expected_version"}
        if not provided:
            raise ValueError("수정할 필드를 하나 이상 입력하세요.")
        for field in {"title", "description", "service", "severity", "detected_at"} & provided:
            if getattr(self, field) is None:
                raise ValueError(f"{field}에 null을 지정할 수 없습니다.")
        return self


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Version
    to_status: TicketStatus
    reason: Note
    resolution: Note | None = None

    @model_validator(mode="after")
    def require_resolution(self):
        if (self.to_status == TicketStatus.RESOLVED) != (self.resolution is not None):
            raise ValueError("해결 상태로 전환할 때만 해결 요약을 입력하세요.")
        return self


class TicketView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    number: str = Field(validation_alias="display_number")
    title: str
    description: str
    service: str
    severity: Severity
    status: TicketStatus
    detected_at: AwareDatetime
    impact: str | None
    next_action: str | None
    recheck_at: AwareDatetime | None
    resolution: str | None
    resolved_at: AwareDatetime | None
    created_by: UUID
    version: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    archived_at: AwareDatetime | None
    allowed_transitions: list[TicketStatus] = Field(default_factory=list)


class TicketPage(BaseModel):
    items: list[TicketView]
    next_cursor: str | None


class TicketCursor(BaseModel):
    updated_at: AwareDatetime
    id: UUID
    fingerprint: str


def view(ticket: Ticket) -> TicketView:
    result = TicketView.model_validate(ticket)
    if ticket.archived_at is None:
        result.allowed_transitions = TRANSITIONS[result.status]
    return result


def ticket_by_id(db: Session, ticket_id: UUID, *, lock: bool = False, run_id: UUID | None = None) -> Ticket:
    statement = select(Ticket).where(Ticket.id == ticket_id, Ticket.simulation_run_id == run_id)
    if lock:
        if run_id is None:
            lock_ticket_snapshot(db)
        statement = statement.with_for_update()
    ticket = db.scalar(statement)
    if ticket is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
    return ticket


def check_version(ticket: Ticket, expected_version: int) -> None:
    if ticket.version != expected_version:
        raise ApiError(409, "VERSION_CONFLICT", "티켓이 변경되었습니다.", {"current_version": ticket.version})


def ensure_editable(ticket: Ticket, expected_version: int) -> None:
    if ticket.archived_at is not None:
        raise ApiError(409, "TICKET_ARCHIVED", "보관된 티켓은 수정할 수 없습니다.")
    check_version(ticket, expected_version)


def record_event(db: Session, ticket: Ticket, actor_id: UUID, event_type: str, details: dict) -> None:
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            ticket_version=ticket.version,
            actor_id=actor_id,
            event_type=event_type,
            details=jsonable_encoder(details),
            created_at=ticket.updated_at,
        )
    )


def apply_changes(
    db: Session, ticket: Ticket, actor_id: UUID, event_type: str, fields: dict, *, reason: str | None = None
) -> None:
    changes = {
        name: {"before": getattr(ticket, name), "after": value}
        for name, value in fields.items()
        if getattr(ticket, name) != value
    }
    if not changes:
        return
    for name, change in changes.items():
        setattr(ticket, name, change["after"])
    ticket.version += 1
    ticket.updated_at = datetime.now(timezone.utc)
    details = {"changes": changes}
    if reason is not None:
        details["reason"] = reason
    record_event(db, ticket, actor_id, event_type, details)
    sync_ticket_index(db, ticket)
    db.commit()


@router.post("", status_code=201)
def create_ticket(body: TicketCreate, response: Response, db: Database, user: WriteUser) -> TicketView:
    ticket = insert_ticket(db, user, body)
    response.headers["Location"] = f"/api/v1/tickets/{ticket.id}"
    return view(ticket)


def insert_ticket(db: Session, user: User, body: TicketCreate, **training_fields) -> Ticket:
    if not training_fields:
        lock_ticket_snapshot(db)
    now = datetime.now(timezone.utc)
    ticket = Ticket(
        **body.model_dump(exclude={"scenario_host_id"}),
        **training_fields,
        status=TicketStatus.OPEN,
        created_by=user.id,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(ticket)
    db.flush()
    record_event(db, ticket, user.id, "created", {"values": body.model_dump(), "status": TicketStatus.OPEN})
    db.commit()
    return ticket


@router.get("")
def list_tickets(
    db: Database,
    user: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: TicketStatus | None = None,
    severity: Severity | None = None,
    scope: Scope = Scope.ACTIVE,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> TicketPage:
    rows, next_cursor = select_ticket_page(db, None, q, status, severity, scope, limit, cursor)
    return TicketPage(items=[view(ticket) for ticket in rows], next_cursor=next_cursor)


def select_ticket_page(db: Session, run_id: UUID | None, q: str | None, status: TicketStatus | None,
                       severity: Severity | None, scope: Scope, limit: int, cursor: str | None):
    search = (q or "").strip()
    filters_key = [search, status, severity, scope]
    if run_id is not None:
        filters_key.append(str(run_id))
    fingerprint = hashlib.sha256(json.dumps(filters_key).encode()).hexdigest()
    statement = select(Ticket).where(Ticket.simulation_run_id == run_id)
    if scope == Scope.ACTIVE:
        statement = statement.where(Ticket.archived_at.is_(None))
    elif scope == Scope.ARCHIVED:
        statement = statement.where(Ticket.archived_at.is_not(None))
    if status:
        statement = statement.where(Ticket.status == status)
    if severity:
        statement = statement.where(Ticket.severity == severity)
    if search:
        filters = [column.icontains(search, autoescape=True) for column in (Ticket.title, Ticket.description, Ticket.service)]
        number = re.fullmatch(r"INC-(\d{1,10})", search, re.IGNORECASE)
        if number and 0 < int(number[1]) <= 2147483647:
            filters.append(Ticket.number == int(number[1]))
        statement = statement.where(or_(*filters))
    if cursor:
        try:
            point = TicketCursor.model_validate_json(base64.urlsafe_b64decode(cursor))
            if point.fingerprint != fingerprint:
                raise ValueError("Cursor filter changed")
        except ValueError as exc:
            raise ApiError(422, "INVALID_CURSOR", "현재 검색 조건에 유효한 커서를 사용하세요.") from exc
        statement = statement.where(tuple_(Ticket.updated_at, Ticket.id) < (point.updated_at, point.id))

    rows = list(db.scalars(statement.order_by(Ticket.updated_at.desc(), Ticket.id.desc()).limit(limit + 1)))
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        point = TicketCursor(updated_at=last.updated_at, id=last.id, fingerprint=fingerprint)
        next_cursor = base64.urlsafe_b64encode(point.model_dump_json().encode()).decode()
    return rows[:limit], next_cursor


@router.get("/{ticket_id}")
def read_ticket(ticket_id: UUID, db: Database, user: CurrentUser) -> TicketView:
    return view(ticket_by_id(db, ticket_id))


@router.patch("/{ticket_id}")
def update_ticket(ticket_id: UUID, body: TicketUpdate, db: Database, user: WriteUser) -> TicketView:
    return view(edit_ticket(db, user, ticket_id, body))


def edit_ticket(db: Session, user: User, ticket_id: UUID, body: TicketUpdate, run_id: UUID | None = None) -> Ticket:
    ticket = ticket_by_id(db, ticket_id, lock=True, run_id=run_id)
    ensure_editable(ticket, body.expected_version)
    fields = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    apply_changes(db, ticket, user.id, "updated", fields)
    return ticket


@router.post("/{ticket_id}/transitions")
def change_status(ticket_id: UUID, body: Transition, db: Database, user: WriteUser) -> TicketView:
    return view(transition_ticket(db, user, ticket_id, body))


def transition_ticket(db: Session, user: User, ticket_id: UUID, body: Transition, run_id: UUID | None = None) -> Ticket:
    ticket = ticket_by_id(db, ticket_id, lock=True, run_id=run_id)
    ensure_editable(ticket, body.expected_version)
    if body.to_status not in TRANSITIONS[TicketStatus(ticket.status)]:
        raise ApiError(409, "INVALID_TRANSITION", "현재 상태에서 선택한 상태로 전환할 수 없습니다.")
    if body.to_status == TicketStatus.CLOSED and db.scalar(
        select(TicketWork.id).where(TicketWork.ticket_id == ticket.id, TicketWork.status != "done").limit(1)
    ):
        raise ApiError(409, "TICKET_WORK_PENDING", "미완료 작업을 정리한 뒤 티켓을 종료하세요.")
    fields = {"status": body.to_status}
    if body.to_status == TicketStatus.RESOLVED:
        fields.update(resolution=body.resolution, resolved_at=datetime.now(timezone.utc))
    elif body.to_status == TicketStatus.IN_PROGRESS:
        fields.update(resolution=None, resolved_at=None)
    apply_changes(db, ticket, user.id, "transitioned", fields, reason=body.reason)
    return ticket


@router.delete("/{ticket_id}", status_code=204)
def archive_ticket(
    ticket_id: UUID, expected_version: Annotated[int, Query(ge=1)], db: Database, user: WriteUser,
) -> None:
    archive(db, user, ticket_id, expected_version)


def archive(db: Session, user: User, ticket_id: UUID, expected_version: int, run_id: UUID | None = None) -> None:
    ticket = ticket_by_id(db, ticket_id, lock=True, run_id=run_id)
    if ticket.archived_at is not None:
        return
    check_version(ticket, expected_version)
    if ticket.status != TicketStatus.CLOSED:
        raise ApiError(409, "TICKET_NOT_CLOSED", "종료된 티켓만 보관할 수 있습니다.")
    apply_changes(db, ticket, user.id, "archived", {"archived_at": datetime.now(timezone.utc)})
