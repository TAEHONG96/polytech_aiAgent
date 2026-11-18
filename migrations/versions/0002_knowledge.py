"""Add SOP storage metadata, an indexing queue, and versioned vectors."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

revision = "0002_knowledge"
down_revision = "0001_ticket_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sop_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("filename", sa.String(180), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 8388608", name="ck_sop_documents_size"),
    )
    op.create_table(
        "index_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_type", sa.String(8), nullable=False),
        sa.Column("sop_id", sa.Uuid(), sa.ForeignKey("sop_documents.id", ondelete="CASCADE"), unique=True),
        sa.Column("ticket_id", sa.Uuid(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), unique=True),
        sa.Column("ticket_version", sa.Integer()),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(50)),
        sa.Column("error_message", sa.String(300)),
        sa.Column("indexed_profile", sa.String(120)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(source_type = 'SOP' AND sop_id IS NOT NULL AND sop_id = id "
            "AND ticket_id IS NULL AND ticket_version IS NULL) OR "
            "(source_type = 'TICKET' AND ticket_id IS NOT NULL AND ticket_id = id "
            "AND sop_id IS NULL AND ticket_version IS NOT NULL AND ticket_version > 0)",
            name="ck_index_jobs_source",
        ),
        sa.CheckConstraint("status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')", name="ck_index_jobs_status"),
        sa.CheckConstraint(
            "(status = 'PROCESSING' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'PROCESSING' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_index_jobs_lease",
        ),
    )
    op.create_index("ix_index_jobs_available", "index_jobs", ["status", "available_at"])
    op.create_index("ix_index_jobs_lease", "index_jobs", ["lease_expires_at"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("index_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("section", sa.String(200)),
        sa.Column("embedding", VECTOR(768), nullable=False),
        sa.Column("embedding_model", sa.String(80), nullable=False),
        sa.Column("embedding_profile", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "chunk_index", name="uq_knowledge_chunks_source_index"),
        sa.CheckConstraint("chunk_index >= 0 AND (page_number IS NULL OR page_number > 0)", name="ck_knowledge_chunks_pos"),
    )
    op.create_index("ix_knowledge_chunks_profile", "knowledge_chunks", ["embedding_profile"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON sop_documents, index_jobs, knowledge_chunks TO app_runtime")
    op.execute(
        "INSERT INTO index_jobs (id, source_type, ticket_id, ticket_version, status, attempts, available_at, updated_at) "
        "SELECT id, 'TICKET', id, version, 'PENDING', 0, now(), now() FROM tickets "
        "WHERE status IN ('resolved', 'closed') AND archived_at IS NULL AND resolution IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_table("knowledge_chunks")
    op.drop_table("index_jobs")
    op.drop_table("sop_documents")
