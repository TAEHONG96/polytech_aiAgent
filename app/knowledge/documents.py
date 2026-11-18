import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.http import ApiError
from app.knowledge.models import IndexJob, IndexStatus, SopDocument, SourceType

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_CHARS = 160_000
MAX_PDF_PAGES = 100
MAX_CHUNKS = 240
MAX_PDF_PAGE_STREAM = 2 * 1024 * 1024
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 180
MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
MIME_TYPES = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown"}
logger = logging.getLogger(__name__)


class DocumentFailure(Exception):
    pass


@dataclass(frozen=True)
class TextChunk:
    content: str
    page_number: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    chunks: list[TextChunk]


def storage_dir() -> Path:
    return Path(os.environ.get("SOP_STORAGE_DIR", "/var/lib/polytech/sops"))


def original_path(document_id: UUID) -> Path:
    return storage_dir() / f"{document_id}.upload"


def remove_original(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("SOP file cleanup failed file=%s type=%s", path.name, type(exc).__name__)


def validate_upload(filename: str | None, content_type: str | None, payload: bytes) -> tuple[str, str]:
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 180 or CONTROL_CHARACTERS.search(name) or any(x in name for x in "\r\n\t\f"):
        raise ApiError(422, "SOP_FILENAME_INVALID", "파일 이름을 확인하세요.")
    mime = MIME_TYPES.get(Path(name).suffix.lower())
    declared_type = (content_type or "").partition(";")[0].strip().lower()
    accepted = {mime, "", "application/octet-stream"}
    if mime == "text/markdown":
        accepted.update({"text/plain", "text/x-markdown", "application/markdown", "application/x-markdown"})
    if mime is None or declared_type not in accepted:
        raise ApiError(415, "SOP_FILE_TYPE", "PDF, TXT 또는 Markdown 파일을 업로드하세요.")
    if not payload:
        raise ApiError(422, "SOP_EMPTY_FILE", "빈 파일은 업로드할 수 없습니다.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "SOP_FILE_TOO_LARGE", "업로드 요청 크기는 최대 8 MiB입니다.")
    if mime == "application/pdf":
        if not payload.startswith(b"%PDF-"):
            raise ApiError(415, "SOP_FILE_TYPE", "PDF 파일 서명을 확인하세요.")
    else:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ApiError(415, "SOP_TEXT_ENCODING", "텍스트 파일을 UTF-8로 저장하세요.") from exc
        if CONTROL_CHARACTERS.search(text):
            raise ApiError(415, "SOP_FILE_TYPE", "텍스트 파일에 바이너리 데이터가 있습니다.")
        if not text.strip():
            raise ApiError(422, "SOP_EMPTY_FILE", "파일에 텍스트를 입력하세요.")
    return name, mime


def register_sop(
    db: Session, *, actor_id: UUID, filename: str | None, content_type: str | None, payload: bytes, title: str | None
) -> tuple[SopDocument, IndexJob]:
    name, mime = validate_upload(filename, content_type, payload)
    title = title.strip() if title is not None else Path(name).stem[:160]
    if not title or len(title) > 160 or CONTROL_CHARACTERS.search(title):
        raise ApiError(422, "SOP_TITLE_INVALID", "문서 제목은 1자 이상 160자 이하입니다.")
    digest = hashlib.sha256(payload).hexdigest()
    now = datetime.now(timezone.utc)
    document = SopDocument(
        id=uuid4(), title=title, filename=name, content_type=mime, content_sha256=digest,
        size_bytes=len(payload), created_by=actor_id, created_at=now, updated_at=now,
    )
    db.add(document)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing_id = db.scalar(select(SopDocument.id).where(SopDocument.content_sha256 == digest))
        if existing_id is not None:
            raise ApiError(409, "SOP_DUPLICATE", "같은 내용의 문서가 등록되어 있습니다.", {"id": str(existing_id)}) from exc
        raise
    job = IndexJob(
        id=document.id, sop_id=document.id, source_type=SourceType.SOP, status=IndexStatus.PENDING,
        attempts=0, available_at=now, updated_at=now,
    )
    db.add(job)
    db.flush()
    path = original_path(document.id)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        db.rollback()
        remove_original(path)
        raise ApiError(503, "SOP_STORAGE_UNAVAILABLE", "원본 파일을 저장할 수 없습니다.") from exc
    db.commit()
    return document, job


def split_text(text: str, *, page: int | None = None, section: str | None = None) -> list[TextChunk]:
    text = text.strip()
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            earliest = start + CHUNK_CHARS // 2
            boundary = text.rfind("\n\n", earliest, end)
            if boundary == -1:
                boundary = text.rfind(" ", earliest, end)
            if boundary != -1:
                end = boundary
        if content := text[start:end].strip():
            chunks.append(TextChunk(content, page, section))
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def markdown_chunks(text: str) -> list[TextChunk]:
    result = []
    section = None
    lines = []
    for line in text.splitlines():
        heading = MARKDOWN_HEADING.match(line)
        if heading:
            result.extend(split_text("\n".join(lines), section=section))
            section = heading[1].strip()[:200]
            lines = []
        lines.append(line)
    result.extend(split_text("\n".join(lines), section=section))
    return result


def pdf_pages(path: Path) -> list[tuple[int, str]]:
    pages = []
    total = 0
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise DocumentFailure("암호화된 PDF는 처리할 수 없습니다.")
            if len(reader.pages) > MAX_PDF_PAGES:
                raise DocumentFailure(f"PDF 페이지 수는 {MAX_PDF_PAGES}쪽까지 지원합니다.")
            for page_index, page in enumerate(reader.pages, start=1):
                contents = page.get_contents()
                if contents is not None and len(contents.get_data()) > MAX_PDF_PAGE_STREAM:
                    raise DocumentFailure("PDF 페이지의 분석 데이터가 처리 한도를 초과했습니다.")
                text = (page.extract_text() or "").strip()
                total += len(text)
                if total > MAX_EXTRACTED_CHARS:
                    raise DocumentFailure("문서 텍스트가 처리 한도를 초과했습니다.")
                if text:
                    pages.append((page_index, text))
    except (PdfReadError, ValueError, RecursionError) as exc:
        raise DocumentFailure("PDF에서 텍스트를 읽을 수 없습니다.") from exc
    return pages


def extract_document(path: Path, mime: str) -> ExtractedDocument:
    if mime == "application/pdf":
        pages = pdf_pages(path)
        if not pages:
            raise DocumentFailure("텍스트를 추출할 수 없습니다. 스캔 PDF의 OCR은 지원 범위에서 제외됩니다.")
        raw_text = "\n\n".join(text for _, text in pages)
        chunks = [chunk for number, text in pages for chunk in split_text(text, page=number)]
    else:
        try:
            raw_text = path.read_text(encoding="utf-8-sig").strip()
        except UnicodeDecodeError as exc:
            raise DocumentFailure("UTF-8 문서에서 텍스트를 읽을 수 없습니다.") from exc
        if len(raw_text) > MAX_EXTRACTED_CHARS:
            raise DocumentFailure("문서 텍스트가 처리 한도를 초과했습니다.")
        chunks = markdown_chunks(raw_text) if mime == "text/markdown" else split_text(raw_text)
    if len(chunks) > MAX_CHUNKS:
        raise DocumentFailure("문서 청크 수가 처리 한도를 초과했습니다.")
    if not chunks:
        raise DocumentFailure("색인할 텍스트가 없습니다.")
    if "\x00" in raw_text:
        raise DocumentFailure("문서에 처리할 수 없는 텍스트가 있습니다.")
    return ExtractedDocument(raw_text, chunks)
