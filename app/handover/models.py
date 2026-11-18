from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ShiftTeam(Base):
    __tablename__ = "shift_teams"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class ShiftMembership(Base):
    __tablename__ = "shift_memberships"
    team_id: Mapped[UUID] = mapped_column(ForeignKey("shift_teams.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    is_lead: Mapped[bool]


class ShiftInstance(Base):
    __tablename__ = "shift_instances"
    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="ck_shift_period"),
        ExcludeConstraint((text("tstzrange(starts_at, ends_at, '[)')"), "&&"), name="ex_shift_overlap"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("shift_teams.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(80))


class TicketWork(Base):
    __tablename__ = "ticket_work"
    __table_args__ = (
        CheckConstraint("status IN ('todo','in_progress','blocked','done')", name="ck_ticket_work_status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("tickets.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class Handover(Base):
    __tablename__ = "handovers"
    __table_args__ = (
        Index("uq_handover_root_shift", "outgoing_shift_id", unique=True, postgresql_where=text("supersedes_id IS NULL")),
        CheckConstraint("outgoing_shift_id <> incoming_shift_id", name="ck_handover_distinct_shifts"),
        CheckConstraint("shift_start_at < shift_end_at", name="ck_handover_period"),
        CheckConstraint("version >= snapshot_version AND snapshot_version >= 1", name="ck_handover_version"),
        CheckConstraint("status IN ('DRAFT','READY','CONFIRMED','ACKNOWLEDGED')", name="ck_handover_status"),
        CheckConstraint(
            "(status IN ('CONFIRMED','ACKNOWLEDGED')) = (confirmed_by IS NOT NULL) "
            "AND (confirmed_by IS NULL) = (confirmed_at IS NULL) "
            "AND (status = 'ACKNOWLEDGED') = (acknowledged_by IS NOT NULL) "
            "AND (acknowledged_by IS NULL) = (acknowledged_at IS NULL)",
            name="ck_handover_confirmation",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    outgoing_shift_id: Mapped[UUID] = mapped_column(ForeignKey("shift_instances.id"))
    incoming_shift_id: Mapped[UUID] = mapped_column(ForeignKey("shift_instances.id"))
    shift_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shift_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("handovers.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int]
    snapshot_version: Mapped[int]
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    acknowledged_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HandoverRevision(Base):
    __tablename__ = "handover_revisions"
    handover_id: Mapped[UUID] = mapped_column(ForeignKey("handovers.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    content_snapshot: Mapped[dict] = mapped_column(JSONB)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16))


class HandoverItem(Base):
    __tablename__ = "handover_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["handover_id", "snapshot_version"], ["handover_revisions.handover_id", "handover_revisions.version"],
        ),
    )
    handover_id: Mapped[UUID] = mapped_column(primary_key=True)
    snapshot_version: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("tickets.id"), primary_key=True)
    ticket_snapshot: Mapped[dict] = mapped_column(JSONB)
