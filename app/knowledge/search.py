import atexit
import os
import re
import unicodedata
from dataclasses import dataclass
from threading import Lock
from uuid import UUID

import httpx2
from sqlalchemy import case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.http import ApiError
from app.knowledge.embedding import GEMINI_SEARCH_PROFILE, EmbeddingFailure, checked_vector
from app.knowledge.indexing import RESOLVED_STATUSES
from app.knowledge.models import IndexJob, IndexStatus, KnowledgeChunk, SopDocument, SourceType
from app.models import Ticket
from app.simulation.models import ScenarioIncident, ScenarioTicketTemplate


_worker_client_lock = Lock()
_worker_client: httpx2.Client | None = None


def embedding_worker_client() -> httpx2.Client:
    global _worker_client
    with _worker_client_lock:
        if _worker_client is None:
            _worker_client = httpx2.Client(timeout=18, trust_env=False)
        return _worker_client


@atexit.register
def close_embedding_worker_client() -> None:
    global _worker_client
    with _worker_client_lock:
        if _worker_client is not None:
            _worker_client.close()
            _worker_client = None


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
        response = embedding_worker_client().post(
            f"{url.rstrip('/')}/query-embedding", json={"query": query, "profile": profile},
        )
        if response.status_code == 429:
            raise ApiError(429, "EMBEDDING_RATE_LIMIT", "임베딩 호출 한도를 초과했습니다.")
        if response.status_code == 409:
            raise ApiError(503, "EMBEDDING_CONFIG", "API와 워커의 검색 임베딩 설정이 일치하지 않습니다.")
        if response.status_code == 503:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            if detail in ("EMBEDDING_CONFIG", "EMBEDDING_INVALID"):
                raise ApiError(503, detail, "검색 임베딩 설정 또는 반환값을 확인해 주세요.")
        response.raise_for_status()
        payload = response.json()
        if payload.get("profile") != profile:
            raise ApiError(503, "EMBEDDING_CONFIG", "API와 워커의 검색 임베딩 설정이 일치하지 않습니다.")
        return checked_vector(payload.get("vector"))
    except (ValueError, TypeError, AttributeError, EmbeddingFailure) as exc:
        raise ApiError(503, "EMBEDDING_INVALID", "워커의 검색 임베딩 반환값을 확인해 주세요.") from exc
    except httpx2.HTTPError as exc:
        raise ApiError(503, "EMBEDDING_UNAVAILABLE", "검색용 임베딩을 생성할 수 없습니다.") from exc


def calibrated_min_score(profile: str) -> float | None:
    return 0.62 if profile == GEMINI_SEARCH_PROFILE else None


def keyword_distance(terms: list[str], title, content):
    title = func.lower(func.coalesce(title, ""))
    content = func.lower(content)
    # Prefer a keyword in the title while still finding procedure-specific terms in the indexed snippet.
    rank = sum(case((func.strpos(title, term) > 0, 1.0),
                    (func.strpos(content, term) > 0, 0.6), else_=0.0) for term in terms)
    return 1.0 - rank / len(terms)


def incident_scope(ticket: Ticket):
    same_incident = select(ScenarioTicketTemplate.id).where(
        ScenarioTicketTemplate.id == ticket.scenario_ticket_template_id,
        ScenarioTicketTemplate.initial_ticket["incident_key"].astext == ScenarioIncident.incident_key,
    ).exists()
    return (ScenarioIncident.scenario_id == ticket.scenario_id, ScenarioIncident.approved.is_(True),
            ScenarioIncident.occurred_at < ticket.detected_at, ~same_incident)


def search_index(
    db: Session, *, vector: list[float] | None, profile: str, source_types: list[SourceType], limit: int,
    min_score: float | None = None, run_id: UUID | None = None, target: Ticket | None = None,
    exclude_ticket_id: UUID | None = None,
    keyword_query: str | None = None,
) -> list[SearchResult]:
    if target is not None and (run_id is None or target.simulation_run_id != run_id):
        raise ValueError("Search target must belong to the scoped run")
    approved_sop = SopDocument.id.is_not(None)
    if run_id is not None:
        approved_sop = approved_sop & SopDocument.training_approved.is_(True)
    ticket_scope = Ticket.simulation_run_id == run_id
    if exclude_ticket_id is not None:
        ticket_scope = ticket_scope & (Ticket.id != exclude_ticket_id)
    if vector is None:
        if run_id is None or keyword_query is None:
            raise ValueError("Keyword fallback requires a scoped training run")
        query = unicodedata.normalize("NFKC", keyword_query).lower()
        terms = list(dict.fromkeys(term for term in re.findall(r"[\w]+", query) if len(term.strip("_")) >= 2))[:10]
        if not terms:
            return []
        distance = keyword_distance(terms, func.coalesce(SopDocument.title, Ticket.title), KnowledgeChunk.content)
    else:
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
                approved_sop,
                (Ticket.version == IndexJob.ticket_version)
                & ticket_scope
                & Ticket.status.in_(RESOLVED_STATUSES)
                & Ticket.archived_at.is_(None)
                & Ticket.resolution.is_not(None),
            ),
        )
    )
    if run_id is not None:
        statement = statement.where(IndexJob.indexed_profile == profile)
    if target is not None and SourceType.INCIDENT in source_types:
        incident_distance = (ScenarioIncident.embedding.cosine_distance(vector) if vector is not None else
                             keyword_distance(terms, ScenarioIncident.title, ScenarioIncident.resolution))
        incidents = select(
            literal(SourceType.INCIDENT.value), ScenarioIncident.id, ScenarioIncident.title, literal(None),
            ScenarioIncident.resolution, literal(0), literal(None), literal(None), literal(None),
            incident_distance, literal(1),
        ).where(*incident_scope(target), ScenarioIncident.embedding_profile == profile)
        statement = union_all(statement, incidents)
    statement = statement.subquery()
    results = []
    ranked_sources = select(statement).where(statement.c.source_rank == 1)
    if vector is None:
        ranked_sources = ranked_sources.where(statement.c.distance < 1)
    elif min_score is not None:
        ranked_sources = ranked_sources.where(statement.c.distance <= 1 - min_score)
    ranked_sources = ranked_sources.order_by(statement.c.distance, statement.c.source_id).limit(limit)
    for row in db.execute(ranked_sources):
        kind = SourceType(row.source_type)
        title = row.ticket_title if kind == SourceType.TICKET else row.sop_title
        if run_id is None:
            source_url = f"/{'knowledge' if kind == SourceType.SOP else 'tickets'}/{row.source_id}"
        else:
            prefix = f"/api/v1/simulation-runs/{run_id}"
            if kind == SourceType.INCIDENT:
                source_url = f"{prefix}/tickets/{target.id}/incident-sources/{row.source_id}"
            else:
                source_url = f"{prefix}/{'sops' if kind == SourceType.SOP else 'tickets'}/{row.source_id}"
        results.append(SearchResult(
            source_type=kind, source_id=row.source_id,
            title=title, content=row.content,
            score=round(1 - float(row.distance), 6), chunk_index=row.chunk_index,
            page_number=row.page_number, section=row.section, ticket_version=row.ticket_version,
            source_url=source_url,
        ))
    return results
