from datetime import datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.auth import CurrentUser, WriteUser
from app.db import Database
from app.http import ApiError
from app.knowledge.documents import MAX_UPLOAD_BYTES, original_path, register_sop, remove_original
from app.knowledge.embedding import MAX_QUERY_LENGTH, EmbeddingFailure, embedding_profile
from app.knowledge.indexing import enqueue_sop, sync_ticket_index, ticket_is_indexable
from app.knowledge.models import IndexJob, IndexStatus, SopDocument, SourceType
from app.knowledge.search import SearchResult, search_index, worker_query_vector
from app.models import Ticket

router = APIRouter(tags=["knowledge"])


class SopSummary(BaseModel):
    id: UUID
    title: str
    filename: str
    content_type: str
    size_bytes: int
    status: IndexStatus
    error_code: str | None
    error_message: str | None
    attempts: int
    indexed_profile: str | None
    created_at: datetime
    updated_at: datetime
    original_url: str


class SopDetail(SopSummary):
    extracted_text: str | None


class SopPage(BaseModel):
    items: list[SopSummary]
    next_cursor: UUID | None


class IndexRequest(BaseModel):
    source_id: UUID
    status: IndexStatus


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_LENGTH)]
    source_types: list[SourceType] = Field(default_factory=lambda: list(SourceType), min_length=1, max_length=2)
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 5


class SearchResponse(BaseModel):
    results: list[SearchResult]
    embedding_profile: str


def _summary(document: SopDocument, job: IndexJob) -> SopSummary:
    return SopSummary(
        id=document.id, title=document.title, filename=document.filename,
        content_type=document.content_type, size_bytes=document.size_bytes, status=job.status,
        error_code=job.error_code, error_message=job.error_message, attempts=job.attempts,
        indexed_profile=job.indexed_profile, created_at=document.created_at, updated_at=job.updated_at,
        original_url=f"/api/v1/sops/{document.id}/original",
    )


def _document(db: Session, sop_id: UUID, *, lock: bool = False) -> tuple[SopDocument, IndexJob]:
    statement = select(SopDocument, IndexJob).join(IndexJob, IndexJob.sop_id == SopDocument.id).where(SopDocument.id == sop_id)
    if lock:
        statement = statement.with_for_update(of=SopDocument)
    result = db.execute(statement).one_or_none()
    if result is None:
        raise ApiError(404, "SOP_NOT_FOUND", "SOP 문서를 찾을 수 없습니다.")
    return result[0], result[1]


def _upload_too_large() -> ApiError:
    return ApiError(413, "SOP_FILE_TOO_LARGE", "업로드 요청 크기는 최대 8 MiB입니다.")


async def _bounded_form_request(request: Request) -> Request:
    media = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media != "multipart/form-data":
        raise ApiError(415, "SOP_MULTIPART_REQUIRED", "multipart/form-data로 파일을 전송하세요.")
    length = request.headers.get("content-length")
    if length and length.isascii() and length.isdecimal() and int(length) > MAX_UPLOAD_BYTES:
        raise _upload_too_large()
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_UPLOAD_BYTES:
            raise _upload_too_large()
        payload.extend(chunk)

    async def receive():
        return {"type": "http.request", "body": bytes(payload), "more_body": False}

    return Request(request.scope, receive)


@router.post("/sops", status_code=201)
async def upload_sop(request: Request, response: Response, db: Database, user: WriteUser) -> SopSummary:
    bounded = await _bounded_form_request(request)
    async with bounded.form(max_files=1, max_fields=1, max_part_size=2048) as form:
        file, title = form.get("file"), form.get("title")
        if set(form) - {"file", "title"} or not isinstance(file, UploadFile) or (title is not None and not isinstance(title, str)):
            raise ApiError(422, "SOP_INVALID_FORM", "file 파일과 선택 항목 title을 확인하세요.")
        document, job = await run_in_threadpool(
            register_sop, db, actor_id=user.id, filename=file.filename, content_type=file.content_type,
            payload=await file.read(), title=title,
        )
    response.headers["Location"] = f"/api/v1/sops/{document.id}"
    return _summary(document, job)


@router.get("/sops")
def list_sops(
    db: Database, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 25, cursor: UUID | None = None
) -> SopPage:
    statement = select(SopDocument, IndexJob).join(IndexJob, IndexJob.sop_id == SopDocument.id)
    if cursor is not None:
        after = db.get(SopDocument, cursor)
        if after is None:
            raise ApiError(422, "INVALID_CURSOR", "문서 목록을 첫 페이지부터 조회하세요.")
        statement = statement.where(tuple_(SopDocument.created_at, SopDocument.id) < (after.created_at, after.id))
    rows = db.execute(statement.order_by(SopDocument.created_at.desc(), SopDocument.id.desc()).limit(limit + 1)).all()
    return SopPage(
        items=[_summary(document, job) for document, job in rows[:limit]],
        next_cursor=rows[limit - 1][0].id if len(rows) > limit else None,
    )


@router.get("/sops/{sop_id}")
def read_sop(sop_id: UUID, db: Database, user: CurrentUser) -> SopDetail:
    document, job = _document(db, sop_id)
    return SopDetail(**_summary(document, job).model_dump(), extracted_text=document.extracted_text)


@router.get("/sops/{sop_id}/original")
def download_sop(sop_id: UUID, db: Database, user: CurrentUser) -> StreamingResponse:
    document, _ = _document(db, sop_id)
    try:
        stream = original_path(document.id).open("rb")
    except FileNotFoundError as exc:
        raise ApiError(404, "SOP_FILE_MISSING", "원본 파일을 찾을 수 없습니다.") from exc

    def read_file():
        with stream:
            yield from iter(lambda: stream.read(65536), b"")

    filename = quote(document.filename, safe="")
    return StreamingResponse(
        read_file(), media_type=document.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.delete("/sops/{sop_id}", status_code=204)
def delete_sop(sop_id: UUID, db: Database, user: WriteUser) -> None:
    document, _ = _document(db, sop_id, lock=True)
    path = original_path(document.id)
    db.delete(document)
    db.commit()
    remove_original(path)


@router.post("/sops/{sop_id}/reindex", status_code=202)
def reindex_sop(sop_id: UUID, db: Database, user: WriteUser) -> IndexRequest:
    document, _ = _document(db, sop_id, lock=True)
    job = enqueue_sop(db, document)
    db.commit()
    return IndexRequest(source_id=job.id, status=job.status)


@router.post("/knowledge/tickets/{ticket_id}/reindex", status_code=202)
def reindex_ticket(ticket_id: UUID, db: Database, user: WriteUser) -> IndexRequest:
    ticket = db.get(Ticket, ticket_id, with_for_update=True)
    if ticket is None:
        raise ApiError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
    if not ticket_is_indexable(ticket):
        raise ApiError(409, "TICKET_NOT_INDEXABLE", "해결 요약이 있는 미보관 해결·종료 티켓을 사용하세요.")
    job = sync_ticket_index(db, ticket, explicitly=True)
    db.commit()
    return IndexRequest(source_id=job.id, status=job.status)


@router.post("/knowledge/search")
def search(body: SearchRequest, db: Database, user: WriteUser) -> SearchResponse:
    try:
        profile = embedding_profile().fingerprint
    except EmbeddingFailure as exc:
        raise ApiError(503, exc.code, str(exc)) from exc
    db.rollback()  # Release the authentication transaction before the worker request.
    vector = worker_query_vector(body.query, profile)
    results = search_index(db, vector=vector, profile=profile, source_types=body.source_types, limit=body.limit)
    return SearchResponse(results=results, embedding_profile=profile)
