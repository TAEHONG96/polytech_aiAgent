from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.knowledge import routes as knowledge
from app.knowledge.embedding import FakeEmbedder
from app.settings import settings
from main import app
from test_simulation import ORIGIN, POSTGRES, start
from test_training_isolation import data_owner, finish, logged_in, request

pytestmark = POSTGRES
SCENARIO = {"scenario_key": "dco-basic", "scenario_version": "1.1"}
PASSWORD = "synthetic-phase3-password"


@pytest.fixture
def students(monkeypatch):
    monkeypatch.setattr(auth_module, "settings", lambda: replace(settings(), training_registration=True))
    with TestClient(app) as alice, TestClient(app) as bob:
        actors = []
        for client in (alice, bob):
            name = f"phase3-{uuid4().hex}"
            account = request(client, "POST", "/api/v1/auth/register", 201, headers=ORIGIN,
                              json={"username": name, "password": PASSWORD}).json()
            headers = logged_in(client, name, PASSWORD, "trainee")
            launched = start(client, headers, scenario=SCENARIO)
            assert launched.status_code == 201, launched.text
            actors.append((client, headers, launched.headers["Location"], account["id"]))
        yield actors


def tickets(who, run):
    return {row["training_origin"]: row for row in request(who, "GET", run + "/tickets", 200).json()["items"]}


def submit(who, headers, path, code, expected, *, key=None, params=None, note=None, version=None):
    body = {"action_code": code, "parameters": params or {}, "note": note, "expected_version": version}
    return request(who, "POST", path + "/actions", expected,
                   headers={**headers, "Idempotency-Key": key or uuid4().hex}, json=body).json()


def test_published_schedule_bot_links_and_same_version_replay(students, data_owner):
    (alice, headers, run, _), (bob, _, other, _) = students
    schedule = request(alice, "GET", run + "/schedule", 200).json()["slots"]
    assert [(slot["crew_code"], slot["type"], slot["actor_type"]) for slot in schedule] == [
        ("D", "NIGHT", "BOT"), ("A", "DAY", "TRAINEE"), ("B", "NIGHT", "BOT"),
    ]
    assert [slot["starts_at"] for slot in schedule] == [
        "2026-11-19T20:00:00+09:00", "2026-11-20T08:00:00+09:00", "2026-11-20T20:00:00+09:00",
    ]
    assert schedule[-1]["ends_at"] == "2026-11-21T08:00:00+09:00"
    assert [slot["ends_at"] for slot in schedule[:-1]] == [slot["starts_at"] for slot in schedule[1:]]
    seeded = tickets(alice, run)
    report = request(alice, "GET", run + "/incoming-handover", 200).json()
    link, = report["tickets"]
    assert request(alice, "GET", link["ticket_url"], 200).json() == seeded["CARRYOVER"]
    request(bob, "GET", link["ticket_url"], 404)
    request(bob, "GET", other + "/tickets/" + seeded["CARRYOVER"]["id"], 404)
    states = request(alice, "GET", run + "/hosts", 200).json()
    memory = run + "/tickets/" + seeded["INITIAL"]["id"]
    submit(alice, headers, memory, "RUN_BMC_DIAGNOSTICS", 201, params={"target": "memory"})
    finish(data_owner, run)
    replay = start(alice, headers, key="phase3-second", scenario=SCENARIO)
    assert replay.status_code == 201
    newer = replay.headers["Location"]
    assert request(alice, "GET", newer + "/hosts", 200).json() == states
    for kind, ticket in tickets(alice, newer).items():
        assert ticket["id"] != seeded[kind]["id"]
        for field in ("title", "description", "status", "detected_at", "scenario_host_id"):
            assert ticket[field] == seeded[kind][field]
    request(alice, "GET", newer + "/tickets/" + seeded["INITIAL"]["id"] + "/actions", 404)
    assert request(alice, "GET", memory + "/actions", 200).json()["activity"]
    submit(alice, headers, memory, "CHECK_SERVER_STATUS", 409)


def test_action_rules_order_memos_and_fixed_time(students):
    (alice, headers, run, actor), (bob, bob_headers, bob_run, _) = students
    item = tickets(alice, run)["INITIAL"]
    path = run + "/tickets/" + item["id"]
    request(bob, "GET", run + "/events", 404)
    submit(bob, bob_headers, path, "CHECK_SERVER_STATUS", 404)
    submit(bob, bob_headers, bob_run + "/tickets/" + item["id"], "CHECK_SERVER_STATUS", 404)
    view = request(alice, "GET", run, 200).json()
    seed = request(alice, "GET", run + "/hosts", 200).json()
    begun = datetime.now(timezone.utc)
    edit = request(alice, "PATCH", path, 422, headers=headers,
                   json={"expected_version": item["version"], "detected_at": "2026-11-22T08:00:00+09:00"})
    assert edit.json()["error"]["code"] == "FIXED_TICKET_TIME"
    assert submit(alice, headers, path, "REPLACE_MEMORY", 409)["error"]["code"] == "ACTION_PRECONDITION_FAILED"
    assert submit(alice, headers, path, "RUN_BMC_DIAGNOSTICS", 422,
                  params={"target": "storage"})["error"]["code"] == "INVALID_ACTION_PARAMETERS"

    memo = submit(alice, headers, path, "CUSTOM_NOTE", 201, note="교체 가능성을 기록했습니다.")
    assert memo["kind"] == "NOTE" and memo["result"] is None
    assert request(alice, "GET", run + "/hosts", 200).json() == seed
    assert submit(alice, headers, path, "CHECK_MEMORY", 409)["error"]["code"] == "ACTION_PRECONDITION_FAILED"
    assert submit(alice, headers, path, "CHECK_SERVER_STATUS", 409,
                  version=item["version"])["error"]["code"] == "VERSION_CONFLICT"

    work = request(alice, "POST", path + "/work", 201, headers=headers, json={
        "expected_version": memo["ticket_version"], "title": "검증 결과를 인계", "status": "todo",
    }).json()
    diagnosed = submit(alice, headers, path, "RUN_BMC_DIAGNOSTICS", 201,
                       key="diagnostic", params={"target": "memory"}, version=work["version"])
    assert diagnosed["result"]["error_code"] == "MEM_ECC_ERROR"
    submit(alice, headers, path, "CHECK_MEMORY", 201)
    replaced = submit(alice, headers, path, "REPLACE_MEMORY", 201)
    assert replaced["result"]["health"] == "verification_pending"
    verified = submit(alice, headers, path, "RERUN_DIAGNOSTICS", 201)
    assert verified["result"]["health"] == "healthy" and verified["result"]["error_code"] is None
    assert submit(alice, headers, path, "RUN_BMC_DIAGNOSTICS", 200,
                  key="diagnostic", params={"target": "memory"}, version=work["version"]) == diagnosed
    assert submit(alice, headers, path, "CHECK_SERVER_STATUS", 409,
                  key="diagnostic")["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    notes_and_actions = request(alice, "GET", path + "/actions", 200).json()["activity"]
    assert [row["action_id"] for row in notes_and_actions].count(diagnosed["action_id"]) == 1
    assert all(row["actor_id"] == actor and datetime.fromisoformat(row["occurred_at"]) >= begun
               for row in notes_and_actions)
    events = request(alice, "GET", run + "/events?limit=2", 200).json()
    page = request(alice, "GET", run + f"/events?after_sequence={events['next_after_sequence']}", 200).json()
    rows = events["items"] + page["items"]
    assert [row["event_sequence"] for row in rows] == list(range(1, len(rows) + 1))
    assert [row["event_type"] for row in rows].count("virtual_action_performed") == 4
    assert "work_created" in [row["event_type"] for row in rows]
    assert request(alice, "GET", run, 200).json() == view
    assert request(alice, "GET", path, 200).json()["status"] == item["status"]


def test_concurrent_action_retry_is_atomically_replayed(students):
    (alice, headers, run, _), _ = students
    path = run + "/tickets/" + tickets(alice, run)["INITIAL"]["id"] + "/actions"
    payload = {"action_code": "RUN_BMC_DIAGNOSTICS", "parameters": {"target": "memory"}}

    def post(_):
        with TestClient(app, cookies=alice.cookies) as contender:
            return contender.post(path, headers={**headers, "Idempotency-Key": "concurrent"}, json=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(post, range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 201]
    assert replies[0].json() == replies[1].json()
    assert len(request(alice, "GET", path, 200).json()["activity"]) == 1


def test_incident_sources_and_rag_scope(students, monkeypatch):
    (alice, headers, run, _), (bob, _, other, _) = students
    item = tickets(alice, run)["INITIAL"]
    path = run + "/tickets/" + item["id"]
    incident_history = request(alice, "GET", path + "/host-incidents?limit=1", 200).json()
    assert incident_history["host"]["id"] == item["scenario_host_id"]
    history = request(alice, "GET", path + "/host-incidents", 200).json()["incidents"]
    case, = history
    assert case["incident_key"] == "SYN-MEM-01"
    request(alice, "GET", case["source_url"], 200)
    request(bob, "GET", case["source_url"], 404)
    for suffix in ("013", "014", "015"):
        request(alice, "GET", path + "/incident-sources/d3000000-0000-4000-8000-000000000" + suffix, 404)
    monkeypatch.setattr(knowledge, "worker_query_vector", lambda query, profile: FakeEmbedder().query(query))
    search = {"query": "합성 메모리 ECC 오류", "source_types": ["INCIDENT"], "ticket_id": item["id"], "limit": 5}
    result = request(alice, "POST", run + "/knowledge/search", 200, headers=headers, json=search).json()
    assert {hit["source_id"] for hit in result["results"]} == {
        "d3000000-0000-4000-8000-000000000011", "d3000000-0000-4000-8000-000000000012",
    }
    request(alice, "POST", run + "/knowledge/search", 404, headers=headers,
            json={**search, "ticket_id": tickets(bob, other)["INITIAL"]["id"]})
    assert request(alice, "POST", run + "/knowledge/search", 200,
                   headers=headers, json={**search, "ticket_id": None}).json()["results"] == []
