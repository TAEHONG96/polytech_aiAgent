"""Preserve training runs while a learner switches to another run."""

import sqlalchemy as sa
from alembic import op

revision = "0009_simulation_pause"
down_revision = "0008_training_evaluation"


def upgrade():
    op.add_column("simulation_runs", sa.Column("start_paused_run_id", sa.Uuid()))
    op.drop_constraint("ck_simulation_run_status", "simulation_runs", type_="check")
    op.create_check_constraint(
        "ck_simulation_run_status", "simulation_runs",
        "(status IN ('ACTIVE', 'PAUSED') AND completed_at IS NULL) "
        "OR (status = 'COMPLETED' AND completed_at IS NOT NULL)",
    )


def downgrade():
    # PostgreSQL rejects the old constraint if any paused runs still need to be preserved.
    op.drop_constraint("ck_simulation_run_status", "simulation_runs", type_="check")
    op.create_check_constraint(
        "ck_simulation_run_status", "simulation_runs",
        "(status = 'ACTIVE' AND completed_at IS NULL) OR (status = 'COMPLETED' AND completed_at IS NOT NULL)",
    )
    op.drop_column("simulation_runs", "start_paused_run_id")
