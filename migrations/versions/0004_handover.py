"""Add shift schedules, ticket work and versioned handovers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB

revision = "0004_handover"
down_revision = "0003_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shift_teams",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
    )
    op.create_table(
        "shift_memberships",
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("shift_teams.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("is_lead", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_shift_memberships_user_id", "shift_memberships", ["user_id"])
    op.create_table(
        "shift_instances",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("shift_teams.id"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False, unique=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False),
        sa.CheckConstraint("starts_at < ends_at", name="ck_shift_period"),
        ExcludeConstraint((sa.text("tstzrange(starts_at, ends_at, '[)')"), "&&"), name="ex_shift_overlap"),
    )
    op.add_column("tickets", sa.Column("shift_instance_id", sa.Uuid()))
    op.add_column("tickets", sa.Column("owner_id", sa.Uuid()))
    op.add_column("tickets", sa.Column("warnings", sa.Text()))
    op.add_column("tickets", sa.Column("approval_required", sa.Boolean()))
    op.create_foreign_key("fk_tickets_shift_instance", "tickets", "shift_instances", ["shift_instance_id"], ["id"])
    op.create_foreign_key("fk_tickets_owner", "tickets", "users", ["owner_id"], ["id"])
    op.create_index("ix_tickets_shift_instance_id", "tickets", ["shift_instance_id"])
    op.create_table(
        "ticket_work",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint("status IN ('todo','in_progress','blocked','done')", name="ck_ticket_work_status"),
    )
    op.create_index("ix_ticket_work_ticket_id", "ticket_work", ["ticket_id"])
    op.create_table(
        "handovers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("outgoing_shift_id", sa.Uuid(), sa.ForeignKey("shift_instances.id"), nullable=False),
        sa.Column("incoming_shift_id", sa.Uuid(), sa.ForeignKey("shift_instances.id"), nullable=False),
        sa.Column("shift_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shift_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), sa.ForeignKey("handovers.id"), unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("confirmed_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("acknowledged_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("outgoing_shift_id <> incoming_shift_id", name="ck_handover_distinct_shifts"),
        sa.CheckConstraint("shift_start_at < shift_end_at", name="ck_handover_period"),
        sa.CheckConstraint("version >= snapshot_version AND snapshot_version >= 1", name="ck_handover_version"),
        sa.CheckConstraint("status IN ('DRAFT','READY','CONFIRMED','ACKNOWLEDGED')", name="ck_handover_status"),
        sa.CheckConstraint(
            "(status IN ('CONFIRMED','ACKNOWLEDGED')) = (confirmed_by IS NOT NULL) "
            "AND (confirmed_by IS NULL) = (confirmed_at IS NULL) "
            "AND (status = 'ACKNOWLEDGED') = (acknowledged_by IS NOT NULL) "
            "AND (acknowledged_by IS NULL) = (acknowledged_at IS NULL)", name="ck_handover_confirmation",
        ),
    )
    op.create_index("ix_handovers_created_at", "handovers", ["created_at"])
    op.create_index("uq_handover_root_shift", "handovers", ["outgoing_shift_id"], unique=True,
                    postgresql_where=sa.text("supersedes_id IS NULL"))
    op.create_table(
        "handover_revisions",
        sa.Column("handover_id", sa.Uuid(), sa.ForeignKey("handovers.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("content_snapshot", JSONB(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
    )
    op.create_table(
        "handover_items",
        sa.Column("handover_id", sa.Uuid(), primary_key=True),
        sa.Column("snapshot_version", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id"), primary_key=True),
        sa.Column("ticket_snapshot", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["handover_id", "snapshot_version"],
                                ["handover_revisions.handover_id", "handover_revisions.version"]),
    )
    op.add_column("agent_runs", sa.Column("handover_id", sa.Uuid()))
    op.add_column("agent_runs", sa.Column("handover_version", sa.Integer()))
    op.create_foreign_key("fk_agent_run_handover_revision", "agent_runs", "handover_revisions",
                          ["handover_id", "handover_version"], ["handover_id", "version"], match="FULL")
    op.create_index("ix_agent_runs_handover", "agent_runs", ["handover_id", "handover_version", "created_at"])
    op.create_index("uq_agent_runs_active_handover", "agent_runs", ["handover_id"], unique=True,
                    postgresql_where=sa.text("handover_id IS NOT NULL AND status IN ('queued', 'running')"))
    op.execute("GRANT SELECT ON shift_teams, shift_memberships, shift_instances TO app_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON handovers, ticket_work TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON handover_items, handover_revisions TO app_runtime")


def downgrade() -> None:
    op.drop_index("uq_agent_runs_active_handover", table_name="agent_runs")
    op.drop_index("ix_agent_runs_handover", table_name="agent_runs")
    op.drop_constraint("fk_agent_run_handover_revision", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "handover_version")
    op.drop_column("agent_runs", "handover_id")
    for table in ("handover_items", "handover_revisions", "handovers", "ticket_work"):
        op.drop_table(table)
    op.drop_index("ix_tickets_shift_instance_id", table_name="tickets")
    for column in ("approval_required", "warnings", "owner_id", "shift_instance_id"):
        op.drop_column("tickets", column)
    for table in ("shift_instances", "shift_memberships", "shift_teams"):
        op.drop_table(table)
