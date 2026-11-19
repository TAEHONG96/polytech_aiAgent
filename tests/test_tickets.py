import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.settings import settings
from main import app

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_TESTS") != "1",
    reason="Run against an isolated PostgreSQL database through the Compose test service.",
)

ORIGIN = {"Origin": os.environ.get("APP_ORIGIN", "http://127.0.0.1:8080")}
USER = os.environ.get("APP_AUTH_USERNAME", "demo")
PASSWORD = os.environ.get("APP_BOOTSTRAP_PASSWORD", "local-demo-password")
TICKETS = "/api/v1/tickets"


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth(client):
    response = client.post(
        "/api/v1/auth/login", headers=ORIGIN, json={"username": USER, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return {**ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def ticket_input(title):
    return {
        "title": title,
        "description": "가상 스토리지 지연",
        "service": "storage",
        "severity": "P2",
        "detected_at": "2026-11-18T09:00:00+09:00",
        "impact": "가상 서비스 응답 지연",
    }


def create(client, auth, title):
    response = client.post(TICKETS, headers=auth, json=ticket_input(title))
    assert response.status_code == 201, response.text
    ticket = response.json()
    assert response.headers["Location"] == f"{TICKETS}/{ticket['id']}"
    assert ticket["number"].startswith("INC-")
    assert ticket["detected_at"] == "2026-11-18T00:00:00Z"
    return ticket


def test_postgres_ready_and_runtime_privileges(client):
    settings()
    assert client.get("/api/v1/ready").json() == {"status": "ready"}
    with engine().connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "app_runtime"
        assert connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        assert connection.scalar(text("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector")) == 0
        assert connection.scalar(text("SELECT NOT has_table_privilege('tickets', 'DELETE')"))
        assert connection.scalar(text("SELECT NOT has_table_privilege('ticket_events', 'UPDATE')"))


def test_session_and_csrf(client):
    assert client.get(TICKETS).status_code == 401
    assert client.post(
        "/api/v1/auth/login", json={"username": USER, "password": PASSWORD}
    ).status_code == 403
    assert client.post(
        "/api/v1/auth/login", headers=ORIGIN, json={"username": USER, "password": "bad password"}
    ).status_code == 401
    login = client.post("/api/v1/auth/login", headers=ORIGIN, json={"username": USER, "password": PASSWORD})
    assert login.status_code == 200
    assert client.get("/api/v1/auth/session").json()["csrf_token"] == login.json()["csrf_token"]
    invalid = client.post(TICKETS, headers=ORIGIN, json=ticket_input("denied"))
    assert invalid.status_code == 403
    assert invalid.json()["error"]["code"] == "CSRF_INVALID"
    assert invalid.headers["X-Request-ID"] == invalid.json()["error"]["request_id"]
    auth = {**ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
    assert client.post("/api/v1/auth/logout", headers=auth).status_code == 204
    assert client.get("/api/v1/auth/session").status_code == 401


def test_ticket_lifecycle_is_persisted_with_audit_events(client, auth):
    ticket = create(client, auth, f"요청 {uuid4()}")
    ticket_id = UUID(ticket["id"])
    path = f"{TICKETS}/{ticket_id}"
    assert ticket["status"] == "open"
    assert ticket["version"] == 1
    assert client.get(path).json() == ticket
    assert client.get(TICKETS, params={"q": ticket["number"]}).json()["items"] == [ticket]

    invalid = client.delete(path, params={"expected_version": 1}, headers=auth)
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "TICKET_NOT_CLOSED"

    updated = client.patch(path, headers=auth, json={"expected_version": 1, "severity": "P1", "impact": None})
    assert updated.status_code == 200, updated.text
    assert updated.json()["impact"] is None
    assert updated.json()["description"] == ticket["description"]
    assert updated.json()["version"] == 2

    stale = client.patch(path, headers=auth, json={"expected_version": 1, "service": "network"})
    assert stale.status_code == 409
    assert stale.json()["error"]["details"] == {"current_version": 2}

    resolved = client.post(
        path + "/transitions",
        headers=auth,
        json={"expected_version": 2, "to_status": "resolved", "reason": "시연", "resolution": "가상 정상화"},
    )
    assert resolved.status_code == 200, resolved.text
    closed = client.post(
        path + "/transitions", headers=auth, json={"expected_version": 3, "to_status": "closed", "reason": "확인"}
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["resolved_at"] == resolved.json()["resolved_at"]

    assert client.delete(path, headers=auth, params={"expected_version": 4}).status_code == 204
    assert client.delete(path, headers=auth, params={"expected_version": 4}).status_code == 204
    archived = client.get(path).json()
    assert archived["archived_at"] is not None
    assert archived["allowed_transitions"] == []
    assert client.get(TICKETS, params={"q": ticket["number"]}).json()["items"] == []
    assert client.get(TICKETS, params={"q": ticket["number"], "scope": "archived"}).json()["items"] == [archived]
    assert client.patch(path, headers=auth, json={"expected_version": 5, "title": "거부"}).status_code == 409

    with engine().connect() as db:
        row = db.execute(text("SELECT severity, version, archived_at FROM tickets WHERE id = :id"), {"id": ticket_id}).one()
        assert (row.severity, row.version) == ("P1", 5)
        assert row.archived_at is not None
        events = db.execute(
            text(
                "SELECT event_type, ticket_version, actor_id FROM ticket_events "
                "WHERE ticket_id = :id ORDER BY ticket_version"
            ),
            {"id": ticket_id},
        ).all()
        assert [(event.event_type, event.ticket_version) for event in events] == [
            ("created", 1), ("updated", 2), ("transitioned", 3), ("transitioned", 4), ("archived", 5),
        ]
        assert {event.actor_id for event in events} == {UUID(ticket["created_by"])}


def test_validation_search_and_cursor(client, auth):
    source = ticket_input("유효성 검사")
    for bad_value in (
        {**source, "title": "  "},
        {**source, "detected_at": "2026-11-18T09:00:00"},
        {**source, "detected_at": 1790000000},
        {**source, "status": "closed"},
    ):
        assert client.post(TICKETS, headers=auth, json=bad_value).status_code == 422

    prefix = f"관련 {uuid4()}"
    first = create(client, auth, prefix + " _검색")
    second = create(client, auth, prefix + " 검색")
    path = f"{TICKETS}/{second['id']}"
    assert client.patch(path, headers=auth, json={"expected_version": 1, "title": None}).status_code == 422
    assert client.get(TICKETS, params={"q": prefix + " _"}).json()["items"] == [first]
    query = {"q": prefix, "limit": 1}
    page = client.get(TICKETS, params=query).json()
    assert page["items"] == [second]
    next_page = client.get(TICKETS, params={**query, "cursor": page["next_cursor"]}).json()
    assert next_page == {"items": [first], "next_cursor": None}
    cursor_mismatch = client.get(TICKETS, params={**query, "status": "open", "cursor": page["next_cursor"]})
    assert cursor_mismatch.status_code == 422
    quoted_title = prefix + " QA '인용' 100%"
    quoted = create(client, auth, quoted_title)
    assert client.get(TICKETS, params={"q": quoted_title}).json()["items"] == [quoted]
    assert client.get(f"{TICKETS}/{uuid4()}").status_code == 404


def test_concurrent_updates_allow_one_version(client, auth):
    ticket = create(client, auth, f"동시 편집 {uuid4()}")
    path = f"{TICKETS}/{ticket['id']}"

    def edit(title):
        return client.patch(path, headers=auth, json={"expected_version": 1, "title": title})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ("변경 A", "변경 B")))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert client.get(path).json()["version"] == 2
