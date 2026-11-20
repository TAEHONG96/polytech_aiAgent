import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.db import engine
from app.knowledge.documents import (
    DocumentFailure, ExtractedDocument, extract_document, original_path, remove_original, split_text, storage_dir,
)
from app.knowledge.embedding import EMBEDDING_BATCH_SIZE, Embedder, EmbeddingFailure, checked_vector, get_embedder
from app.knowledge.models import IndexJob, IndexStatus, KnowledgeChunk, SopDocument, SourceType
from app.models import Ticket, TicketEvent

RESOLVED_STATUSES = ("resolved", "closed")
ACTIVE_STATES = (IndexStatus.PENDING, IndexStatus.PROCESSING)
MAX_ATTEMPTS = 3
LEASE_TIME = timedelta(minutes=3)
logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ticket_is_indexable(ticket: Ticket) -> bool:
    return (
        ticket.simulation_run_id is None and ticket.status in RESOLVED_STATUSES
        and ticket.archived_at is None and bool(ticket.resolution)
    )


def _reset(job: IndexJob, now: datetime) -> None:
    job.status = IndexStatus.PENDING
    job.attempts = 0
    job.available_at = now
    job.lease_token = None
    job.lease_expires_at = None
    job.error_code = None
    job.error_message = None
    job.indexed_profile = None
    job.updated_at = now


def enqueue_sop(db: Session, document: SopDocument) -> IndexJob:
    job = db.get(IndexJob, document.id, with_for_update=True, populate_existing=True)
    if job.status not in ACTIVE_STATES:
        _reset(job, now_utc())
    return job


def sync_ticket_index(db: Session, ticket: Ticket, *, explicitly: bool = False) -> IndexJob | None:
    job = db.get(IndexJob, ticket.id, with_for_update=True)
    if not ticket_is_indexable(ticket):
        if job is not None:
            db.delete(job)
        return None
    if job is None:
        job = IndexJob(id=ticket.id, source_type=SourceType.TICKET, ticket_id=ticket.id)
        db.add(job)
    elif job.ticket_version == ticket.version and (not explicitly or job.status in ACTIVE_STATES):
        return job
    job.ticket_version = ticket.version
    _reset(job, now_utc())
    return job


@dataclass(frozen=True)
class Claim:
    source_id: UUID
    source_type: SourceType
    ticket_version: int | None
    lease_token: UUID
    attempts: int


@dataclass(frozen=True)
class IndexInput:
    title: str
    document: ExtractedDocument


class LostClaim(Exception):
    pass


def _claim() -> Claim | None:
    now = now_utc()
    with Session(engine()) as db, db.begin():
        job = db.scalar(
            select(IndexJob)
            .where(or_(
                (IndexJob.status == IndexStatus.PENDING) & (IndexJob.available_at <= now),
                (IndexJob.status == IndexStatus.PROCESSING) & (IndexJob.lease_expires_at <= now),
            ))
            .order_by(IndexJob.available_at, IndexJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status = IndexStatus.FAILED
            job.error_code = "INDEX_LEASE_EXPIRED"
            job.error_message = "처리 시간이 초과되었습니다. 재처리를 요청할 수 있습니다."
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = now
            return None
        token = uuid4()
        job.status = IndexStatus.PROCESSING
        job.attempts += 1
        job.lease_token = token
        job.lease_expires_at = now + LEASE_TIME
        job.updated_at = now
        return Claim(job.id, SourceType(job.source_type), job.ticket_version, token, job.attempts)


def _owned_job(db: Session, claim: Claim) -> IndexJob:
    job = db.get(IndexJob, claim.source_id, with_for_update=True)
    if job is None or job.lease_token != claim.lease_token:
        raise LostClaim()
    return job


def _refresh_lease(claim: Claim) -> None:
    with Session(engine()) as db, db.begin():
        job = _owned_job(db, claim)
        job.lease_expires_at = now_utc() + LEASE_TIME


def _ticket_text(db: Session, ticket: Ticket) -> IndexInput:
    fields = [
        ("장애 제목", ticket.title), ("서비스·장비", ticket.service), ("장애 증상·오류 정보", ticket.description),
        ("영향 범위", ticket.impact), ("진단·다음 조치", ticket.next_action), ("조치 결과", ticket.resolution),
    ]
    transitions = db.scalars(
        select(TicketEvent.details)
        .where(TicketEvent.ticket_id == ticket.id, TicketEvent.event_type == "transitioned")
        .order_by(TicketEvent.ticket_version.desc())
        .limit(5)
    )
    reasons = [details.get("reason") for details in transitions]
    fields.extend(("상태 변경 기록", str(reason)[:1000]) for reason in reversed(reasons) if reason)
    text = "\n\n".join(f"{label}\n{value}" for label, value in fields if value)
    return IndexInput(ticket.title, ExtractedDocument(text, split_text(text)))


def _input(claim: Claim) -> IndexInput:
    with Session(engine()) as db:
        if claim.source_type == SourceType.SOP:
            document = db.get(SopDocument, claim.source_id)
            if document is None:
                raise LostClaim()
            title, mime = document.title, document.content_type
        else:
            ticket = db.get(Ticket, claim.source_id)
            if ticket is None or ticket.version != claim.ticket_version or not ticket_is_indexable(ticket):
                raise LostClaim()
            return _ticket_text(db, ticket)
    return IndexInput(title, extract_document(original_path(claim.source_id), mime))


def _publish(claim: Claim, data: ExtractedDocument, vectors: list[list[float]], embedder: Embedder) -> None:
    with Session(engine()) as db, db.begin():
        # Business mutations lock the original row before the job.
        if claim.source_type == SourceType.SOP:
            document = db.get(SopDocument, claim.source_id, with_for_update=True)
            if document is None:
                raise LostClaim()
        else:
            document = None
            ticket = db.get(Ticket, claim.source_id, with_for_update=True)
            if ticket is None or ticket.version != claim.ticket_version or not ticket_is_indexable(ticket):
                raise LostClaim()
        job = _owned_job(db, claim)
        now = now_utc()
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == claim.source_id))
        for index, (chunk, vector) in enumerate(zip(data.chunks, vectors, strict=True)):
            db.add(KnowledgeChunk(
                source_id=claim.source_id, chunk_index=index, content=chunk.content,
                page_number=chunk.page_number, section=chunk.section, embedding=vector,
                embedding_model=embedder.profile.model, embedding_profile=embedder.profile.fingerprint, created_at=now,
            ))
        if document is not None:
            document.extracted_text = data.text
            document.updated_at = now
        job.status = IndexStatus.COMPLETED
        job.indexed_profile = embedder.profile.fingerprint
        job.error_code = None
        job.error_message = None
        job.lease_token = None
        job.lease_expires_at = None
        job.updated_at = now


def _fail(claim: Claim, code: str, message: str, retryable: bool) -> None:
    try:
        with Session(engine()) as db, db.begin():
            job = _owned_job(db, claim)
            now = now_utc()
            retry = retryable and claim.attempts < MAX_ATTEMPTS
            job.status = IndexStatus.PENDING if retry else IndexStatus.FAILED
            job.available_at = now + timedelta(seconds=5 * 2 ** (claim.attempts - 1)) if retry else now
            job.error_code = code
            job.error_message = message
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = now
    except LostClaim:
        return
    logger.warning("knowledge index failure source=%s attempt=%s code=%s", claim.source_id, claim.attempts, code)


def process_next(embedder: Embedder | None = None) -> bool:
    claim = _claim()
    if claim is None:
        return False
    try:
        source = _input(claim)
        provider = embedder or get_embedder()
        vectors = []
        chunks = source.document.chunks
        for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
            _refresh_lease(claim)
            batch = [chunk.content for chunk in chunks[start:start + EMBEDDING_BATCH_SIZE]]
            embeddings = provider.documents(source.title, batch)
            if len(embeddings) != len(batch):
                raise EmbeddingFailure("EMBEDDING_INVALID", "임베딩 개수가 입력과 일치하지 않습니다.")
            vectors.extend(checked_vector(values) for values in embeddings)
        _publish(claim, source.document, vectors, provider)
    except LostClaim:
        pass
    except DocumentFailure as exc:
        _fail(claim, "SOP_EXTRACTION_FAILED", str(exc), False)
    except EmbeddingFailure as exc:
        _fail(claim, exc.code, str(exc), exc.retryable)
    except FileNotFoundError:
        _fail(claim, "SOP_FILE_MISSING", "원본 파일을 찾을 수 없습니다.", False)
    except Exception as exc:
        logger.warning("knowledge indexing error source=%s type=%s", claim.source_id, type(exc).__name__)
        _fail(claim, "INDEX_FAILED", "문서 또는 티켓을 색인할 수 없습니다.", True)
    return True


def clean_orphan_files() -> None:
    cutoff = time.time() - 3600
    with Session(engine()) as db:
        for path in storage_dir().glob("*.upload"):
            try:
                document_id = UUID(path.stem)
                if path.stat().st_mtime < cutoff and db.get(SopDocument, document_id) is None:
                    remove_original(path)
            except (ValueError, FileNotFoundError):
                continue
