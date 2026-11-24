from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_companion_tutor"
down_revision = "0010_companion_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "companion_tutor_offers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("login_scope", sa.String(64), nullable=False),
        sa.Column("tab_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id", "ticket_id"], ["tickets.simulation_run_id", "tickets.id"],
                                ondelete="CASCADE", name="fk_tutor_offer_ticket"),
    )
    op.create_index("ix_tutor_offer_scope", "companion_tutor_offers", ["user_id", "login_scope", "tab_id", "run_id", "created_at"])
    op.create_index("ix_tutor_offer_created", "companion_tutor_offers", ["created_at"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON companion_tutor_offers TO app_runtime")


def downgrade():
    op.drop_table("companion_tutor_offers")
