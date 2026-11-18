import os
from dataclasses import dataclass
from uuid import UUID

import httpx2
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.http import ApiError
from app.knowledge.embedding import EmbeddingFailure, checked_vector
from app.knowledge.indexing import RESOLVED_STATUSES
from app.knowledge.models import IndexJob, IndexStatus, KnowledgeChunk, SopDocument, SourceType
from app.models import Ticket


@dataclass(frozen=True)
class SearchResult:
    source_type: SourceType
    source_id: UUID
    title: str
    content: str
    score: float
    chunk_index: int
    page_number: int | None
    section: str | None
    ticket_version: int | None
    source_url: str


def worker_query_vector(query: str, profile: str) -> list[float]:
    url = os.environ.get("KNOWLEDGE_WORKER_URL", "http://worker:8001")
    try:
        with httpx2.Client(timeout=18, trust_env=False) as client:
            response = client.post(f"{url.rstrip('/')}/query-embedding", json={"query": query, "profile": profile})
        if response.status_code == 429:
            raise ApiError(429, "EMBEDDING_RATE_LIMIT", "임베딩 호출 한도를 초과했습니다.")
        response.raise_for_status()
        payload = response.json()
        if payload.get("profile") != profile:
            raise ValueError("Embedding profile mismatch")
        return checked_vector(payload.get("vector"))
    except (httpx2.HTTPError, ValueError, TypeError, AttributeError, EmbeddingFailure) as exc:
        raise ApiError(503, "EMBEDDING_UNAVAILABLE", "검색용 임베딩을 생성할 수 없습니다.") from exc


def search_index(
    db: Session, *, vector: list[float], profile: str, source_types: list[SourceType], limit: int
) -> list[SearchResult]:
    distance = KnowledgeChunk.embedding.cosine_distance(vector)
    statement = (
        select(
            IndexJob.source_type, KnowledgeChunk.source_id, SopDocument.title.label("sop_title"),
            Ticket.title.label("ticket_title"), KnowledgeChunk.content, KnowledgeChunk.chunk_index,
            KnowledgeChunk.page_number, KnowledgeChunk.section, IndexJob.ticket_version, distance.label("distance"),
            func.row_number().over(
                partition_by=KnowledgeChunk.source_id, order_by=[distance, KnowledgeChunk.chunk_index]
            ).label("source_rank"),
        )
        .join(IndexJob, IndexJob.id == KnowledgeChunk.source_id)
        .outerjoin(SopDocument, SopDocument.id == IndexJob.sop_id)
        .outerjoin(Ticket, Ticket.id == IndexJob.ticket_id)
        .where(
            IndexJob.status == IndexStatus.COMPLETED,
            KnowledgeChunk.embedding_profile == profile,
            IndexJob.source_type.in_(source_types),
            or_(
                SopDocument.id.is_not(None),
                (Ticket.version == IndexJob.ticket_version)
                & Ticket.status.in_(RESOLVED_STATUSES)
                & Ticket.archived_at.is_(None)
                & Ticket.resolution.is_not(None),
            ),
        )
        .subquery()
    )
    results = []
    ranked_sources = select(statement).where(statement.c.source_rank == 1).order_by(
        statement.c.distance, statement.c.source_id
    ).limit(limit)
    for row in db.execute(ranked_sources):
        is_sop = row.source_type == SourceType.SOP
        results.append(SearchResult(
            source_type=SourceType(row.source_type), source_id=row.source_id,
            title=row.sop_title if is_sop else row.ticket_title, content=row.content,
            score=round(1 - float(row.distance), 6), chunk_index=row.chunk_index,
            page_number=row.page_number, section=row.section, ticket_version=row.ticket_version,
            source_url=f"/{'knowledge' if is_sop else 'tickets'}/{row.source_id}",
        ))
    return results
