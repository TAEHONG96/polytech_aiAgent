from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, SmallInteger,
    String, Text, UniqueConstraint, false, text,
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
        ForeignKeyConstraint(("finalized_handover_id", "finalized_revision"),
                             ("handover_revisions.handover_id", "handover_revisions.version"),
                             name="fk_run_final_revision", match="FULL"),
        CheckConstraint("finalized_handover_id IS NULL OR status='COMPLETED'", name="ck_run_final_state"),
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
    last_event_sequence: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    finalized_handover_id: Mapped[UUID | None]
    finalized_revision: Mapped[int | None]


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


class ScenarioIncident(Base):
    __tablename__ = "scenario_incidents"
    __table_args__ = (
        ForeignKeyConstraint(["scenario_id", "scenario_host_id"], ["scenario_hosts.scenario_id", "scenario_hosts.id"],
                             name="fk_scenario_incidents_host"),
        CheckConstraint("(embedding IS NULL) = (embedding_profile IS NULL)", name="ck_scenario_incidents_embedding"),
        UniqueConstraint("scenario_id", "incident_key", name="uq_scenario_incident_key"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    scenario_id: Mapped[UUID]
    scenario_host_id: Mapped[UUID]
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(String(160))
    incident_key: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    resolution: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(server_default=false())
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(768))
    embedding_profile: Mapped[str | None] = mapped_column(String(160))


class SimulationEvaluation(Base):
    __tablename__ = "simulation_evaluations"
    __table_args__ = (
        ForeignKeyConstraint(["handover_id", "handover_revision"],
                             ["handover_revisions.handover_id", "handover_revisions.version"],
                             name="fk_sim_evaluation_revision"),
        CheckConstraint("rules_status IN ('PENDING','PROCESSING','READY','FAILED')", name="ck_sim_evaluation_rules"),
        CheckConstraint("explanation_status IN ('NOT_STARTED','PENDING','PROCESSING','READY','FAILED')",
                        name="ck_sim_evaluation_explanation"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    simulation_run_id: Mapped[UUID] = mapped_column(ForeignKey("simulation_runs.id"), unique=True)
    handover_id: Mapped[UUID]
    handover_revision: Mapped[int]
    rule_version: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict] = mapped_column(JSONB)
    rules_status: Mapped[str] = mapped_column(String(12))
    explanation_status: Mapped[str] = mapped_column(String(12))
    rule_summary: Mapped[dict | None] = mapped_column(JSONB)
    natural_language_summary: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finalize_key: Mapped[str | None] = mapped_column(String(128))
    finalize_digest: Mapped[str | None] = mapped_column(String(64))
    finalize_response: Mapped[dict | None] = mapped_column(JSONB)
    rules_attempts: Mapped[int] = mapped_column(server_default=text("0"))
    rules_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rules_lease_token: Mapped[UUID | None]
    rules_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rules_error: Mapped[str | None] = mapped_column(String(64))
    explanation_attempts: Mapped[int] = mapped_column(server_default=text("0"))
    explanation_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    explanation_lease_token: Mapped[UUID | None]
    explanation_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    explanation_error: Mapped[str | None] = mapped_column(String(64))


class SimulationTicketEvaluation(Base):
    __tablename__ = "simulation_ticket_evaluations"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "ticket_id", name="uq_sim_evaluation_ticket"),
        CheckConstraint("activity_status IN ('ATTEMPTED','NOT_ATTEMPTED')", name="ck_sim_evaluation_activity"),
        CheckConstraint("final_status IN ('open','in_progress','waiting','resolved','closed')",
                        name="ck_sim_evaluation_final_status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    evaluation_id: Mapped[UUID] = mapped_column(ForeignKey("simulation_evaluations.id"))
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("tickets.id"))
    activity_status: Mapped[str] = mapped_column(String(14))
    final_status: Mapped[str] = mapped_column(String(20))
    rule_results: Mapped[list | None] = mapped_column(JSONB)
    evidence: Mapped[list | None] = mapped_column(JSONB)
    explanation: Mapped[dict | None] = mapped_column(JSONB)


class LearningEvent(Base):
    __tablename__ = "simulation_learning_events"
    __table_args__ = (
        ForeignKeyConstraint(("simulation_run_id", "ticket_id"), ("tickets.simulation_run_id", "tickets.id"),
                             name="fk_learning_run_ticket"),
        UniqueConstraint("simulation_run_id", "event_sequence", name="uq_learning_sequence"),
        UniqueConstraint("simulation_run_id", "ticket_id", "kind", "source_id", "activity", name="uq_learning_evidence"),
        CheckConstraint("kind IN ('HOST_HISTORY','SOP') AND activity IN ('VIEWED','USED') "
                        "AND (activity='USED') = (action_event_id IS NOT NULL)", name="ck_learning_activity"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    simulation_run_id: Mapped[UUID]
    ticket_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(12))
    source_id: Mapped[UUID]
    activity: Mapped[str] = mapped_column(String(6))
    action_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("ticket_events.id"))
