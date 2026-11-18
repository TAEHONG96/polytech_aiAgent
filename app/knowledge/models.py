from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

EMBEDDING_DIM = 768


class SourceType(StrEnum):
    SOP = "SOP"
    TICKET = "TICKET"


class IndexStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SopDocument(Base):
    __tablename__ = "sop_documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0 AND size_bytes <= 8388608", name="ck_sop_documents_size"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(160))
    filename: Mapped[str] = mapped_column(String(180))
    content_type: Mapped[str] = mapped_column(String(80))
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int]
    extracted_text: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IndexJob(Base):
    __tablename__ = "index_jobs"
    __table_args__ = (
        CheckConstraint(
            "(source_type = 'SOP' AND sop_id IS NOT NULL AND sop_id = id "
            "AND ticket_id IS NULL AND ticket_version IS NULL) OR "
            "(source_type = 'TICKET' AND ticket_id IS NOT NULL AND ticket_id = id "
            "AND sop_id IS NULL AND ticket_version IS NOT NULL AND ticket_version > 0)",
            name="ck_index_jobs_source",
        ),
        CheckConstraint("status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')", name="ck_index_jobs_status"),
        CheckConstraint(
            "(status = 'PROCESSING' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'PROCESSING' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_index_jobs_lease",
        ),
        Index("ix_index_jobs_available", "status", "available_at"),
        Index("ix_index_jobs_lease", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(8))
    sop_id: Mapped[UUID | None] = mapped_column(ForeignKey("sop_documents.id", ondelete="CASCADE"), unique=True)
    ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), unique=True)
    ticket_version: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(12))
    attempts: Mapped[int]
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(300))
    indexed_profile: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "chunk_index", name="uq_knowledge_chunks_source_index"),
        CheckConstraint("chunk_index >= 0 AND (page_number IS NULL OR page_number > 0)", name="ck_knowledge_chunks_pos"),
        Index("ix_knowledge_chunks_profile", "embedding_profile"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("index_jobs.id", ondelete="CASCADE"))
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    page_number: Mapped[int | None]
    section: Mapped[str | None] = mapped_column(String(200))
    embedding: Mapped[list[float]] = mapped_column(VECTOR(EMBEDDING_DIM))
    embedding_model: Mapped[str] = mapped_column(String(80))
    embedding_profile: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
