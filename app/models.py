from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginSession(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("severity IN ('P1', 'P2', 'P3', 'P4')", name="ck_tickets_severity"),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting', 'resolved', 'closed')",
            name="ck_tickets_status",
        ),
        CheckConstraint(
            "(status IN ('resolved', 'closed')) = (resolution IS NOT NULL) "
            "AND (resolution IS NULL) = (resolved_at IS NULL)",
            name="ck_tickets_resolution",
        ),
        CheckConstraint("archived_at IS NULL OR status = 'closed'", name="ck_tickets_archive"),
        CheckConstraint("version >= 1", name="ck_tickets_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    number: Mapped[int] = mapped_column(Integer, Identity(), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    service: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(String(20))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    impact: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    recheck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shift_instance_id: Mapped[UUID | None] = mapped_column(ForeignKey("shift_instances.id"), index=True)
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    warnings: Mapped[str | None] = mapped_column(Text)
    approval_required: Mapped[bool | None]
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def display_number(self) -> str:
        return f"INC-{self.number:06d}"


Index("ix_tickets_updated_id", Ticket.updated_at.desc(), Ticket.id.desc())
Index(
    "ix_tickets_active_updated_id",
    Ticket.updated_at.desc(),
    Ticket.id.desc(),
    postgresql_where=Ticket.archived_at.is_(None),
)


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (UniqueConstraint("ticket_id", "ticket_version", name="uq_ticket_events_version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("tickets.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    ticket_version: Mapped[int]
    event_type: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
