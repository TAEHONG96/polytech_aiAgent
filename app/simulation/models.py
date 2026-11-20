from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, SmallInteger,
    String, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Scenario(Base):
    __tablename__ = "simulation_scenarios"
    __table_args__ = (UniqueConstraint("scenario_key", "version", name="uq_scenario_version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    scenario_key: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    timezone: Mapped[str] = mapped_column(String(64))
    virtual_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_published: Mapped[bool]
    config: Mapped[dict] = mapped_column(JSONB)


class ScenarioShiftSlot(Base):
    __tablename__ = "scenario_shift_slots"
    __table_args__ = (
        UniqueConstraint("scenario_id", "slot_order", name="uq_scenario_slot_order"),
        CheckConstraint(
            "slot_order BETWEEN 0 AND 2 AND crew_code IN ('A', 'B', 'C', 'D') "
            "AND shift_type IN ('DAY', 'NIGHT') AND starts_at < ends_at "
            "AND actor_type = CASE WHEN slot_order = 1 THEN 'TRAINEE' ELSE 'BOT' END",
            name="ck_scenario_slot",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    scenario_id: Mapped[UUID] = mapped_column(ForeignKey("simulation_scenarios.id"))
    slot_order: Mapped[int] = mapped_column(SmallInteger)
    crew_code: Mapped[str] = mapped_column(String(1))
    shift_type: Mapped[str] = mapped_column(String(5))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_type: Mapped[str] = mapped_column(String(7))


class ScenarioHost(Base):
    __tablename__ = "scenario_hosts"
    __table_args__ = (
        UniqueConstraint("scenario_id", "host_key", name="uq_scenario_host_key"),
        UniqueConstraint("scenario_id", "id", name="uq_scenario_host_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    scenario_id: Mapped[UUID] = mapped_column(ForeignKey("simulation_scenarios.id"))
    host_key: Mapped[str] = mapped_column(String(64))
    hostname: Mapped[str] = mapped_column(String(160))
    aliases: Mapped[list] = mapped_column(JSONB)
    initial_state: Mapped[dict] = mapped_column(JSONB)


class ScenarioTicketTemplate(Base):
    __tablename__ = "scenario_ticket_templates"
    __table_args__ = (
        UniqueConstraint("scenario_id", "template_key", name="uq_scenario_ticket_key"),
        UniqueConstraint("scenario_id", "scenario_host_id", "id", "origin", name="uq_scenario_ticket_source"),
        ForeignKeyConstraint(
            ["scenario_id", "scenario_host_id"], ["scenario_hosts.scenario_id", "scenario_hosts.id"],
            name="fk_scenario_ticket_host",
        ),
        CheckConstraint("origin IN ('INITIAL', 'CARRYOVER')", name="ck_scenario_ticket_origin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    scenario_id: Mapped[UUID]
    scenario_host_id: Mapped[UUID]
    template_key: Mapped[str] = mapped_column(String(64))
    initial_ticket: Mapped[dict] = mapped_column(JSONB)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str] = mapped_column(String(10))


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    __table_args__ = (
        UniqueConstraint("id", "scenario_id", name="uq_simulation_run_scenario"),
        UniqueConstraint("user_id", "start_request_key", name="uq_simulation_run_request"),
        CheckConstraint(
            "(status = 'ACTIVE' AND completed_at IS NULL) "
            "OR (status = 'COMPLETED' AND completed_at IS NOT NULL)",
            name="ck_simulation_run_status",
        ),
        Index("uq_simulation_run_active", "user_id", unique=True, postgresql_where=text("status = 'ACTIVE'")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    scenario_id: Mapped[UUID] = mapped_column(ForeignKey("simulation_scenarios.id"))
    status: Mapped[str] = mapped_column(String(9))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incoming_report_snapshot: Mapped[dict] = mapped_column(JSONB)
    start_request_key: Mapped[str] = mapped_column(String(128))


class SimulationRunHost(Base):
    __tablename__ = "simulation_run_hosts"
    __table_args__ = (
        UniqueConstraint("simulation_run_id", "scenario_id", "scenario_host_id", name="uq_run_host_scenario"),
        ForeignKeyConstraint(
            ["simulation_run_id", "scenario_id"], ["simulation_runs.id", "simulation_runs.scenario_id"],
            name="fk_run_host_run",
        ),
        ForeignKeyConstraint(
            ["scenario_id", "scenario_host_id"], ["scenario_hosts.scenario_id", "scenario_hosts.id"],
            name="fk_run_host_template",
        ),
    )

    simulation_run_id: Mapped[UUID] = mapped_column(primary_key=True)
    scenario_host_id: Mapped[UUID] = mapped_column(primary_key=True)
    scenario_id: Mapped[UUID]
    state: Mapped[dict] = mapped_column(JSONB)
