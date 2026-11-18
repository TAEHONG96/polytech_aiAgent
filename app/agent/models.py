from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_agent_runs_status"),
        Index("ix_agent_runs_queue", "status", "created_at"),
        ForeignKeyConstraint(
            ["handover_id", "handover_version"], ["handover_revisions.handover_id", "handover_revisions.version"],
            name="fk_agent_run_handover_revision", match="FULL",
        ),
        Index("ix_agent_runs_handover", "handover_id", "handover_version", "created_at"),
        Index("uq_agent_runs_active_handover", "handover_id", unique=True,
              postgresql_where=text("handover_id IS NOT NULL AND status IN ('queued', 'running')")),
        Index(
            "uq_agent_runs_active_user", "requested_by", unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("tickets.id"))
    handover_id: Mapped[UUID | None]
    handover_version: Mapped[int | None]
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(14))
    provider: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[dict | None] = mapped_column(JSONB)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(300))
    retryable: Mapped[bool] = mapped_column(default=False)
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_agent_tool_calls_sequence"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    sequence: Mapped[int]
    name: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict] = mapped_column(JSONB)
    elapsed_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
