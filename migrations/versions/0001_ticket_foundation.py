"""Enable pgvector and create the ticket foundation.

Revision ID: 0001_ticket_foundation
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_ticket_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("number", sa.Integer(), sa.Identity(), nullable=False, unique=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("service", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("impact", sa.Text()),
        sa.Column("next_action", sa.Text()),
        sa.Column("recheck_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("severity IN ('P1', 'P2', 'P3', 'P4')", name="ck_tickets_severity"),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting', 'resolved', 'closed')",
            name="ck_tickets_status",
        ),
        sa.CheckConstraint(
            "(status IN ('resolved', 'closed')) = (resolution IS NOT NULL) "
            "AND (resolution IS NULL) = (resolved_at IS NULL)",
            name="ck_tickets_resolution",
        ),
        sa.CheckConstraint("archived_at IS NULL OR status = 'closed'", name="ck_tickets_archive"),
        sa.CheckConstraint("version >= 1", name="ck_tickets_version"),
    )
    order = [sa.text("updated_at DESC"), sa.text("id DESC")]
    op.create_index("ix_tickets_updated_id", "tickets", order)
    op.create_index(
        "ix_tickets_active_updated_id", "tickets", order, postgresql_where=sa.text("archived_at IS NULL")
    )
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("ticket_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ticket_id", "ticket_version", name="uq_ticket_events_version"),
    )
    op.execute("GRANT USAGE ON SCHEMA public TO app_runtime")
    op.execute("GRANT SELECT ON alembic_version, users TO app_runtime")
    op.execute("GRANT SELECT, INSERT, DELETE ON sessions TO app_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON tickets TO app_runtime")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE tickets_number_seq TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON ticket_events TO app_runtime")


def downgrade() -> None:
    op.drop_table("ticket_events")
    op.drop_table("tickets")
    op.drop_table("sessions")
    op.drop_table("users")
    op.execute("DROP EXTENSION vector")
