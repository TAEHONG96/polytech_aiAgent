from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class TutorOffer(Base):
    __tablename__ = "companion_tutor_offers"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "ticket_id"], ["tickets.simulation_run_id", "tickets.id"],
                             ondelete="CASCADE", name="fk_tutor_offer_ticket"),
        Index("ix_tutor_offer_scope", "user_id", "login_scope", "tab_id", "run_id", "created_at"),
        Index("ix_tutor_offer_created", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    login_scope: Mapped[str] = mapped_column(String(64))
    tab_id: Mapped[UUID]
    run_id: Mapped[UUID]
    ticket_id: Mapped[UUID]
    context: Mapped[dict] = mapped_column(JSONB)
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
