import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from google.genai import errors
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db import engine
from app.knowledge import demo, routes, search
from app.knowledge.documents import CHUNK_CHARS, CHUNK_OVERLAP, MAX_UPLOAD_BYTES, extract_document, original_path
from app.knowledge.embedding import EMBEDDING_DIM, EmbeddingFailure, EmbeddingProfile, FakeEmbedder, GeminiEmbedder
from app.knowledge.indexing import MAX_ATTEMPTS, now_utc, process_next
from app.knowledge.models import IndexJob, IndexStatus, KnowledgeChunk
from main import app

ORIGIN = os.environ.get("APP_ORIGIN", "http://127.0.0.1:8080")
SOPS = "/api/v1/sops"
SEARCH = "/api/v1/knowledge/search"
postgres = pytest.mark.skipif(os.environ.get("RUN_POSTGRES_TESTS") != "1", reason="Requires isolated PostgreSQL")


@pytest.fixture
def operator(tmp_path, monkeypatch):
    monkeypatch.setenv("SOP_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(routes, "worker_query_vector", lambda query, profile: FakeEmbedder().query(query))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login", headers={"Origin": ORIGIN},
            json={
                "username": os.environ.get("APP_AUTH_USERNAME", "demo"),
                "password": os.environ.get("APP_BOOTSTRAP_PASSWORD", "local-demo-password"),
            },
        )
        assert response.status_code == 200, response.text
        yield client, {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def upload(operator, content, filename="demo.md", mime="text/markdown", title=None):
    client, headers = operator
    return client.post(
        SOPS, headers=headers, files={"file": (filename, content, mime)},
        data={"title": title} if title else None,
    )


def drain():
    for _ in range(500):
        if not process_next(FakeEmbedder()):
            return
    pytest.fail("Index queue did not drain")


def find(operator, query, source_types=None, limit=5):
    client, headers = operator
    body = {"query": query, "limit": limit}
    if source_types is not None:
        body["source_types"] = source_types
    response = client.post(SEARCH, headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def make_pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 10 20 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources "
        b"<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents 5 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    blob = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(blob))
        blob.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(blob)
    blob.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        blob.extend(f"{offset:010d} 00000 n \n".encode())
    blob.extend(f"trailer\n<< /Root 1 0 R /Size 6 >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(blob)


def test_markdown_chunking_keeps_sections_and_overlap(tmp_path):
    path = tmp_path / "source.md"
    content = "# Synthetic\n\n## Diagnostics\n" + "abcdefghijklmnopqrstuvwxyz" * 100
    path.write_text(content, encoding="utf-8")
    result = extract_document(path, "text/markdown")
    assert result.text == content
    diagnostics = [chunk for chunk in result.chunks if chunk.section == "Diagnostics"]
    assert len(diagnostics) > 1
    assert all(len(chunk.content) <= CHUNK_CHARS for chunk in diagnostics)
    assert diagnostics[0].content[-CHUNK_OVERLAP:] == diagnostics[1].content[:CHUNK_OVERLAP]


def test_sdk_uses_separate_content_and_checks_output():
    calls = []

    def embed_content(**options):
        calls.append(options)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] * EMBEDDING_DIM) for _ in options["contents"]])

    profile = EmbeddingProfile("gemini", "gemini-embedding-2", "test")
    models = SimpleNamespace(embed_content=embed_content)
    provider = GeminiEmbedder(SimpleNamespace(models=models), profile)
    assert len(provider.documents("SOP", ["summary one", "summary two"])) == 2
    assert len(provider.query("boot diagnostics")) == EMBEDDING_DIM
    assert [content.parts[0].text for content in calls[0]["contents"]] == [
        "title: SOP | text: summary one", "title: SOP | text: summary two",
    ]
    assert calls[0]["config"].output_dimensionality == EMBEDDING_DIM
    assert calls[0]["config"].task_type is None
    assert calls[1]["contents"][0].parts[0].text == "task: search result | query: boot diagnostics"

    models.embed_content = lambda **kwargs: SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0])])
    with pytest.raises(EmbeddingFailure) as failure:
        provider.query("query")
    assert failure.value.code == "EMBEDDING_INVALID"

    def rate_limited(**kwargs):
        raise errors.ClientError(429, {"error": {"code": 429, "message": "vendor data"}})

    models.embed_content = rate_limited
    with pytest.raises(EmbeddingFailure) as failure:
        provider.query("query")
    assert failure.value.code == "EMBEDDING_RATE_LIMIT" and failure.value.retryable
    assert "vendor data" not in str(failure.value)


@pytest.mark.parametrize(("upstream", "detail", "status", "code"), [
    (429, None, 429, "EMBEDDING_RATE_LIMIT"),
    (409, None, 503, "EMBEDDING_CONFIG"),
    (503, None, 503, "EMBEDDING_UNAVAILABLE"),
    (503, "EMBEDDING_CONFIG", 503, "EMBEDDING_CONFIG"),
    (503, "EMBEDDING_INVALID", 503, "EMBEDDING_INVALID"),
    (200, None, 503, "EMBEDDING_INVALID"),
])
def test_worker_http_error_mapping(monkeypatch, upstream, detail, status, code):
    response = httpx2.Response(upstream, json={"detail": detail}) if detail else httpx2.Response(upstream, text="vendor data")
    transport = httpx2.MockTransport(lambda request: response)
    from app.http import ApiError

    with httpx2.Client(transport=transport) as client:
        monkeypatch.setattr(search, "embedding_worker_client", lambda: client)
        with pytest.raises(ApiError) as failure:
            search.worker_query_vector("test", "test-profile")
    assert (failure.value.status, failure.value.code) == (status, code)
    assert "vendor data" not in str(failure.value)


def test_worker_reuses_one_http_client_across_searches(monkeypatch):
    profile = "test-profile"
    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={
        "profile": profile, "vector": vector,
    }))
    created = []
    with httpx2.Client(transport=transport) as client:
        def create_client(**options):
            created.append(options)
            return client

        monkeypatch.setattr(search, "_worker_client", None)
        monkeypatch.setattr(search.httpx2, "Client", create_client)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda query: search.worker_query_vector(query, profile),
                                    ("one", "two", "three", "four", "five")))
        assert results == [vector] * 5
        assert created == [{"timeout": 18, "trust_env": False}]


@postgres
def test_sop_upload_index_search_and_delete(operator):
    client, headers = operator
    marker = f"diagnostics-{uuid4().hex}"
    raw = f"# 모의 진단\n\n## 증상\nServer motherboard POST {marker}\n".encode()
    response = upload(operator, raw)
    assert response.status_code == 201, response.text
    document = response.json()
    sop_id = UUID(document["id"])
    assert document["status"] == "PENDING"
    assert response.headers["Location"] == f"{SOPS}/{sop_id}"
    assert client.get(SOPS).json()["items"][0]["id"] == str(sop_id)
    assert original_path(sop_id).read_bytes() == raw
    duplicate = upload(operator, raw, filename="another-name.md")
    assert duplicate.status_code == 409 and duplicate.json()["error"]["details"]["id"] == str(sop_id)
    assert str(sop_id) not in {row["source_id"] for row in find(operator, marker)}

    drain()
    detail = client.get(f"{SOPS}/{sop_id}").json()
    assert detail["status"] == "COMPLETED"
    assert marker in detail["extracted_text"]
    original = client.get(detail["original_url"])
    assert original.content == raw and "attachment" in original.headers["content-disposition"]
    hit = find(operator, marker, ["SOP"], limit=1)[0]
    assert hit["source_id"] == str(sop_id)
    assert hit["source_url"] == f"/knowledge/{sop_id}"
    assert hit["section"] == "증상"
    assert hit["content"] and -1 <= hit["score"] <= 1
    assert str(sop_id) not in {row["source_id"] for row in find(operator, marker, ["TICKET"])}

    with Session(engine()) as db:
        chunks = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == sop_id)).all()
        assert len(chunks) == 2
        assert all(len(chunk.embedding) == EMBEDDING_DIM and chunk.embedding_model == "fake-lexical-v1" for chunk in chunks)
    assert client.post(f"{SOPS}/{sop_id}/reindex", headers=headers).status_code == 202
    drain()
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.source_id == sop_id)) == 2
    assert client.delete(f"{SOPS}/{sop_id}", headers=headers).status_code == 204
    assert client.get(detail["original_url"]).status_code == 404
    assert not original_path(sop_id).exists()
    assert str(sop_id) not in {row["source_id"] for row in find(operator, marker)}
    with Session(engine()) as db:
        assert db.get(IndexJob, sop_id) is None
        assert db.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.source_id == sop_id)) == 0


@postgres
def test_sop_validation_and_pdf_provenance(operator):
    client, headers = operator
    invalid = [
        ("broken.exe", b"data", "application/octet-stream", 415),
        ("broken.pdf", b"fake PDF", "application/pdf", 415),
        ("broken.txt", b"\xff\xfe", "text/plain", 415),
        ("big.txt", b"x" * MAX_UPLOAD_BYTES, "text/plain", 413),
    ]
    for filename, payload, mime, status in invalid:
        assert upload(operator, payload, filename, mime).status_code == status
    denied = client.post(SOPS, headers={"Origin": ORIGIN}, files={"file": ("test.txt", b"text")})
    assert denied.status_code == 403
    marker = "pdfsource" + uuid4().hex
    pdf = upload(operator, make_pdf(f"Synthetic storage {marker}"), "study.pdf", "application/pdf")
    assert pdf.status_code == 201, pdf.text
    scan = upload(operator, make_pdf(""), "scan.pdf", "application/pdf")
    assert scan.status_code == 201, scan.text
    plain = upload(operator, b"\xef\xbb\xbfPlain synthetic text " + marker.encode(), "plain.txt", "text/plain")
    assert plain.status_code == 201, plain.text
    drain()
    sop_id = pdf.json()["id"]
    matches = find(operator, marker, ["SOP"], limit=10)
    assert next(hit for hit in matches if hit["source_id"] == sop_id)["page_number"] == 2
    scan_path = f"{SOPS}/{scan.json()['id']}"
    assert client.get(scan_path).json()["error_code"] == "SOP_EXTRACTION_FAILED"
    assert client.post(scan_path + "/reindex", headers=headers).json()["status"] == "PENDING"
    for result in (pdf, scan, plain):
        assert client.delete(f"{SOPS}/{result.json()['id']}", headers=headers).status_code == 204


@postgres
def test_resolved_ticket_version_is_current_in_search(operator):
    client, headers = operator
    marker = "ticketkeyword" + uuid4().hex
    response = client.post(
        "/api/v1/tickets", headers=headers, json={
            "title": marker, "description": "Synthetic RAID failure", "service": "storage",
            "severity": "P2", "detected_at": "2026-11-18T09:00:00+09:00",
        }
    )
    assert response.status_code == 201
    ticket_id = response.json()["id"]
    ticket_path = f"/api/v1/tickets/{ticket_id}"
    reindex_path = f"/api/v1/knowledge/tickets/{ticket_id}/reindex"
    assert client.post(reindex_path, headers=headers).json()["error"]["code"] == "TICKET_NOT_INDEXABLE"
    resolved = client.post(
        f"{ticket_path}/transitions", headers=headers, json={
            "expected_version": 1, "to_status": "resolved", "reason": "Synthetic diagnostics",
            "resolution": "Rebuilt simulation RAID array",
        }
    )
    assert resolved.status_code == 200, resolved.text
    drain()
    found = find(operator, marker, ["TICKET"], limit=1)[0]
    assert found["source_id"] == ticket_id and found["ticket_version"] == 2
    assert found["source_url"] == f"/tickets/{ticket_id}"
    assert client.get(ticket_path).json()["id"] == found["source_id"]
    assert client.post(reindex_path, headers=headers).status_code == 202
    drain()
    assert client.patch(ticket_path, headers=headers, json={"expected_version": 2, "impact": "Synthetic only"}).status_code == 200
    assert ticket_id not in {hit["source_id"] for hit in find(operator, marker)}
    drain()
    assert find(operator, marker, ["TICKET"], limit=1)[0]["ticket_version"] == 3
    reopened = client.post(
        f"{ticket_path}/transitions", headers=headers,
        json={"expected_version": 3, "to_status": "in_progress", "reason": "Reopen test"},
    )
    assert reopened.status_code == 200
    assert ticket_id not in {hit["source_id"] for hit in find(operator, marker)}


@postgres
def test_retry_backoff_and_explicit_recovery(operator):
    class ThrottledProvider(FakeEmbedder):
        def documents(self, title, texts):
            raise EmbeddingFailure("EMBEDDING_RATE_LIMIT", "Gemini rate limited", retryable=True)

    drain()
    client, headers = operator
    result = upload(operator, f"rate-limit-{uuid4()}".encode(), filename="retry.txt", mime="text/plain")
    assert result.status_code == 201
    sop_id = UUID(result.json()["id"])
    endpoint = f"{SOPS}/{sop_id}"
    for index in range(MAX_ATTEMPTS):
        assert process_next(ThrottledProvider())
        status = client.get(endpoint).json()
        assert status["attempts"] == index + 1
        assert status["error_code"] == "EMBEDDING_RATE_LIMIT"
        if index < MAX_ATTEMPTS - 1:
            assert status["status"] == "PENDING"
            with Session(engine()) as db, db.begin():
                assert db.get(IndexJob, sop_id).available_at > now_utc()
                db.execute(update(IndexJob).where(IndexJob.id == sop_id).values(available_at=now_utc() - timedelta(seconds=1)))
    assert status["status"] == "FAILED"
    assert client.post(endpoint + "/reindex", headers=headers).json()["status"] == "PENDING"
    drain()
    assert client.get(endpoint).json()["status"] == "COMPLETED"
    assert client.delete(endpoint, headers=headers).status_code == 204


@postgres
def test_seed_and_recall_at_five(operator, monkeypatch, capsys):
    username = os.environ.get("APP_AUTH_USERNAME", "demo")
    demo.seed(username)
    assert demo.seed(username) == (0, 0)
    drain()
    monkeypatch.setattr(demo, "worker_query_vector", lambda query, profile: FakeEmbedder().query(query))
    assert demo.evaluate() >= 0.8
    assert "Recall@5=" in capsys.readouterr().out
