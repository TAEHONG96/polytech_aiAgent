import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from google.genai import errors
from google.genai.types import GenerateContentConfig
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.agent import tools
from app.agent.models import AgentRun, AgentToolCall
from app.agent.provider import AnalysisFailure, FakeGenerator, GeminiGenerator
from app.agent.queue import process_agent_run
from app.agent.schemas import GroundingError
from app.auth import SESSION_COOKIE, token_hash
from app.db import engine, get_db
from app.handover.analysis import Draft, render_draft, source_records
from app.handover.models import HandoverItem, HandoverRevision, TicketWork
from app.handover.smoke import ITEM
from app.knowledge import routes
from app.knowledge.documents import original_path, validate_upload
from app.knowledge.embedding import FakeEmbedder
from app.knowledge.models import KnowledgeChunk
from app.models import LoginSession, Ticket, TicketEvent
from main import app
from test_agent import drain_knowledge
from test_handover import only, postgres_required, request, schedule, ticket
from test_tickets import client


def item_in(report, ticket_id):
    return next(item for item in report["items"] if item["id"] == ticket_id)


def draft(who, shift, ticket_id):
    return request(who, "POST", "/handovers", expected=201, **only(who, shift, ticket_id))


def reviewed(who, report, ticket_id):
    body = {
        "summary": "합성 점검 결과와 다음 교대 작업",
        "gap_responses": [{"gap_id": gap["id"], "answer": "합성 점검 기록을 인수조에서 확인 예정"}
                          for gap in item_in(report, ticket_id)["gaps"]],
        "item_notes": [{"ticket_id": ticket_id, "followup": "다음 교대에서 합성 상태를 재확인"}],
    }
    path = "/handovers/" + report["id"]
    saved = request(who, "PUT", path, expected_version=report["version"], body=body)
    assert saved["body"]["gap_responses"] == body["gap_responses"]
    ready = request(who, "PUT", path, expected_version=saved["version"], status="READY")
    assert ready["status"] == "READY"
    return ready


@postgres_required
def test_incident_to_acknowledged_snapshot(schedule, monkeypatch, tmp_path):
    clients, shifts, users, _, _ = schedule
    operator, lead, incoming = (clients[role] for role in ("member", "lead", "incoming"))
    marker = "syntheticPost" + uuid4().hex
    description = f"{marker} POST B101 모의 서버 보드 진단 중 기동 중단"
    monkeypatch.setenv("SOP_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(routes, "worker_query_vector", lambda query, _: FakeEmbedder().query(query))
    assert os.environ["AGENT_PROVIDER"] == "fake"

    historical = ticket(operator, title=marker, description=description)
    historical_id = historical["id"]
    request(operator, "POST", f"/tickets/{historical_id}/transitions", expected_version=1,
            to_status="resolved", resolution=description, reason="합성 해결 기록")
    client, headers = operator
    upload = client.post("/api/v1/sops", headers=headers, files={
        "file": ("qa.md", f"# {marker}\n\n{description}", "text/markdown"),
    })
    assert upload.status_code == 201, upload.text
    sop_id = upload.json()["id"]
    drain_knowledge()

    current = ticket(operator, title=marker + " active", description=description, severity="P1")
    ticket_id = current["id"]
    path = "/tickets/" + ticket_id
    assert current["status"] == "open" and current["created_by"] == users["member"][1]
    for source_type, source_id in (("SOP", sop_id), ("TICKET", historical_id)):
        results = request(operator, "POST", "/knowledge/search", query=marker, source_types=[source_type])
        assert source_id in {hit["source_id"] for hit in results["results"]}
    queued = request(operator, "POST", "/agent/analyze", expected=202, message=description, ticket_id=ticket_id)
    assert process_agent_run()
    run = request(operator, "GET", f"/agent/runs/{queued['run_id']}")
    assert run["status"] == "succeeded", run
    assert [entry["name"] for entry in run["tool_calls"]] == [
        "get_ticket_details", "search_similar_tickets", "search_sop",
    ]
    assert {ticket_id, historical_id, sop_id} <= {item["source_id"] for item in run["result"]["evidence"]}
    assert all(run["result"][field] for field in
               ("confirmed_facts", "hypotheses", "diagnostic_steps", "additional_information"))
    for evidence in run["result"]["evidence"]:
        assert evidence["source_url"].startswith(("/tickets/", "/knowledge/"))

    work = request(operator, "POST", path + "/work", expected=201, expected_version=1,
                   title="합성 보드 점검 비교", status="done")
    pending = request(operator, "POST", path + "/work", expected=201, expected_version=2,
                      title="다음 교대의 합성 보드 점검", status="todo")
    work_id = work["work"][0]["id"]
    pending_id = next(task["id"] for task in pending["work"] if task["status"] == "todo")
    first = draft(operator, shifts[0], ticket_id)
    report_path = "/handovers/" + first["id"]
    gaps = {gap["field"] for gap in item_in(first, ticket_id)["gaps"]}
    assert {"impact", "owner", "shift", "work_due", "work_owner"} <= gaps
    assert request(operator, "PUT", report_path, expected=422, expected_version=1, status="READY",
                   body={"summary": "누락 답변 확인"})["error"]["code"] == "REPORT_INCOMPLETE"
    assert request(operator, "GET", report_path)["version"] == 1

    request(operator, "PATCH", path, expected_version=3, impact="합성 장비 한 대",
            next_action="다음 교대의 합성 보드 점검")
    request(operator, "PUT", path + "/operations", expected_version=4, shift_instance_id=shifts[0],
            owner_id=users["member"][1], warnings="합성 장비에서만 확인", approval_required=False)
    updated = request(operator, "PUT", report_path, expected_version=1, refresh_snapshot=True)
    assert {gap["field"] for gap in item_in(updated, ticket_id)["gaps"]} == {"work_due", "work_owner"}
    request(operator, "POST", report_path + "/analyze", expected=202, expected_version=updated["version"])
    assert process_agent_run()
    analysis = request(operator, "GET", report_path)["analysis"]
    assert analysis["status"] == "succeeded", analysis
    assert [entry["work_id"] for entry in analysis["result"]["completed_work"]] == [work_id]
    assert pending_id in {entry.get("work_id") for entry in analysis["result"]["followups"]}
    assert request(operator, "GET", path)["status"] == "open"

    ready = reviewed(operator, updated, ticket_id)
    confirmed = request(lead, "POST", report_path + "/confirm", expected_version=ready["version"])
    acknowledged = request(incoming, "POST", report_path + "/acknowledge", expected_version=confirmed["version"])
    assert acknowledged["status"] == "ACKNOWLEDGED"
    assert acknowledged["acknowledged_by"] == users["incoming"][1]
    request(incoming, "POST", report_path + "/acknowledge", expected=409, expected_version=acknowledged["version"])
    request(operator, "PATCH", path, expected_version=5, impact="원본 정보 후속 수정")
    stored = request(incoming, "GET", report_path)
    assert stored["items"] == acknowledged["items"] == confirmed["items"]
    assert item_in(stored, ticket_id)["impact"] == "합성 장비 한 대"

    with Session(engine()) as db:
        ticket_uuid, report_uuid, run_uuid = map(UUID, (ticket_id, first["id"], queued["run_id"]))
        assert db.get(Ticket, ticket_uuid).impact == "원본 정보 후속 수정"
        assert db.get(TicketWork, UUID(pending_id)).ticket_id == ticket_uuid
        assert db.get(AgentRun, run_uuid).ticket_id == ticket_uuid
        assert db.scalar(select(func.count()).select_from(AgentToolCall).where(AgentToolCall.run_id == run_uuid)) == 3
        history = list(db.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket_uuid)
                                  .order_by(TicketEvent.ticket_version)))
        assert [event.ticket_version for event in history] == list(range(1, 7))
        assert db.get(HandoverRevision, (report_uuid, confirmed["version"])).kind == "confirmed"
        item = db.get(HandoverItem, (report_uuid, stored["snapshot_version"], ticket_uuid))
        assert item.ticket_snapshot == item_in(confirmed, ticket_id)
        assert db.scalar(select(func.vector_dims(KnowledgeChunk.embedding))
                         .where(KnowledgeChunk.source_id == UUID(sop_id)).limit(1)) == 768
    assert client.delete(f"/api/v1/sops/{sop_id}", headers=headers).status_code == 204


@postgres_required
def test_incomplete_incident_requests_observations_without_external_evidence(schedule, monkeypatch):
    clients, *_ = schedule
    operator = clients["member"]
    monkeypatch.setattr(tools, "search_index", lambda *_args, **_kwargs: [])
    current = ticket(operator, title="합성 장비 이상", description="서버 화면이 잠깐 멈췄으며 오류 코드를 보지 못했습니다.")
    run = request(operator, "POST", "/agent/analyze", expected=202,
                  message=current["description"], ticket_id=current["id"])
    assert process_agent_run(FakeGenerator)
    result = request(operator, "GET", f"/agent/runs/{run['run_id']}")
    assert result["status"] == "succeeded"
    assert [call["details"]["source_count"] for call in result["tool_calls"][1:]] == [0, 0]
    report = result["result"]
    assert {(source["kind"], source["source_id"]) for source in report["evidence"]} == {
        ("TICKET_DETAILS", current["id"]),
    }
    assert report["additional_information"] and not report["hypotheses"] and not report["diagnostic_steps"]


@postgres_required
def test_long_running_ticket_across_three_shifts(schedule):
    clients, shifts, users, *_ = schedule
    lead, incoming = clients["lead"], clients["incoming"]
    current = ticket(lead, next_action="다음 교대에서 가상 지표 비교")
    ticket_id = current["id"]
    path = "/tickets/" + ticket_id
    first_snapshot = None
    version = 1
    for index, (outgoing, receiver) in enumerate(((lead, incoming), (incoming, lead), (lead, incoming))):
        owner = users["incoming" if index == 1 else "lead"][1]
        request(outgoing, "PUT", path + "/operations", expected_version=version,
                shift_instance_id=shifts[index], owner_id=owner)
        request(outgoing, "POST", path + "/work", expected=201, expected_version=version + 1,
                title="합성 지표 비교", status="done" if index == 0 else "in_progress", owner_id=owner)
        version += 2
        report = draft(outgoing, shifts[index], ticket_id)
        assert item_in(report, ticket_id)["owner_id"] == owner
        assert len(item_in(report, ticket_id)["work"]) == index + 1
        ready = reviewed(outgoing, report, ticket_id)
        report_path = "/handovers/" + report["id"]
        frozen = request(outgoing, "POST", report_path + "/confirm", expected_version=ready["version"])
        request(receiver, "POST", report_path + "/acknowledge", expected_version=frozen["version"])
        if index == 0:
            first_snapshot = (report_path, frozen["items"])
        if index == 1:
            preview = request(receiver, "GET", f"/shift-instances/{shifts[2]}/handover-preview")
            assert "carryover" in item_in(preview, ticket_id)["reasons"]
    assert request(lead, "GET", path)["version"] == version
    path, items = first_snapshot
    assert request(incoming, "GET", path)["items"] == items


@postgres_required
def test_expired_session_and_failed_fk_transaction(schedule):
    clients, _, _, admin, _ = schedule
    operator = clients["member"]
    current = ticket(operator)
    ticket_id = UUID(current["id"])
    client, _ = operator
    session_id = token_hash(client.cookies.get(SESSION_COOKIE))
    with Session(admin) as db, db.begin():
        db.execute(update(LoginSession).where(LoginSession.token_hash == session_id)
                   .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    request(operator, "GET", "/auth/session", expected=401)
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, "synthetic-invalid-token")
    request(operator, "GET", "/auth/session", expected=401)
    with Session(admin) as db:
        row = db.get(Ticket, ticket_id)
        row.owner_id = uuid4()
        with pytest.raises(IntegrityError) as failure:
            db.flush()
        assert failure.value.orig.sqlstate == "23503"
        db.rollback()
        assert db.get(Ticket, ticket_id).owner_id is None


def test_database_failure_returns_sanitized_503(client, monkeypatch, caplog):
    marker = "qa-sensitive-connection-value"

    def fail_db():
        raise OperationalError(None, None, RuntimeError(marker), hide_parameters=True)

    monkeypatch.setitem(app.dependency_overrides, get_db, fail_db)
    response = client.get("/api/v1/auth/session")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DB_UNAVAILABLE"
    assert marker not in response.text + caplog.text
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize(("http_status", "code", "retryable"), [
    (401, "AI_CONFIGURATION", False),
    (429, "AI_RATE_LIMIT", True),
    (503, "AI_UNAVAILABLE", True),
])
def test_gemini_provider_error_translation(http_status, code, retryable):
    async def upstream(**_kwargs):
        raise errors.APIError(http_status, {"error": {"message": "synthetic provider failure"}}, None)

    model = GeminiGenerator.__new__(GeminiGenerator)
    model.model = "synthetic-model"
    model.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=upstream)))
    with pytest.raises(AnalysisFailure) as failure:
        asyncio.run(model.generate([], GenerateContentConfig()))
    assert (failure.value.code, failure.value.retryable) == (code, retryable)
    assert "synthetic provider failure" not in str(failure.value)


def test_pending_work_cannot_be_cited_as_completed():
    item = {**ITEM, "work": [{"id": str(uuid4()), "title": "합성 재점검", "status": "blocked", "note": None}]}
    record = next(record for record in source_records(item) if "work_id" in record)
    key = "H1.1"
    draft = Draft(highlights=[], completed_work=[key], followups=[], warnings=[], needs_review=[], references=[],
                  possible_related_groups=[])
    with pytest.raises(GroundingError):
        render_draft(draft.model_dump_json(), {key: record}, False)
    valid = {**draft.model_dump(), "completed_work": [], "followups": [key]}
    assert render_draft(json.dumps(valid), {key: record}, False)["followups"] == [record]


def test_upload_keeps_display_filename_separate_from_disk_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SOP_STORAGE_DIR", str(tmp_path))
    name, _ = validate_upload(r"fixture\nested/sop.txt", "text/plain", b"Synthetic fixture")
    identifier = uuid4()
    assert name == "sop.txt"
    assert original_path(identifier) == tmp_path / f"{identifier}.upload"
