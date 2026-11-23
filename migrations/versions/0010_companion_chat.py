from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_companion_chat"
down_revision = "0009_simulation_pause"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("companion", postgresql.JSONB(), nullable=True))
    op.create_index("ix_agent_runs_companion_user_created", "agent_runs", ["requested_by", "created_at"],
                    postgresql_where=sa.text("companion IS NOT NULL"))


def downgrade():
    op.execute("DELETE FROM agent_runs WHERE companion IS NOT NULL")
    op.drop_index("ix_agent_runs_companion_user_created", table_name="agent_runs")
    op.drop_column("agent_runs", "companion")
