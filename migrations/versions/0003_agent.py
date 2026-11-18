"""Add durable analysis runs and redacted Function Calling traces."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_agent"
down_revision = "0002_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requested_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id")),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(14), nullable=False),
        sa.Column("provider", sa.String(16)),
        sa.Column("model", sa.String(80)),
        sa.Column("result", JSONB()),
        sa.Column("usage", JSONB()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.String(300)),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_agent_runs_status"),
    )
    op.create_index("ix_agent_runs_queue", "agent_runs", ["status", "created_at"])
    op.create_index(
        "uq_agent_runs_active_user", "agent_runs", ["requested_by"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_tool_calls_sequence"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON agent_runs TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON agent_tool_calls TO app_runtime")


def downgrade() -> None:
    op.drop_table("agent_tool_calls")
    op.drop_table("agent_runs")
