import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import password_hasher
from app.db import engine
from app.models import Ticket, User
from app.simulation import routes
from app.simulation.models import SimulationRun, SimulationRunHost
from main import app

BASE = "/api/v1/simulation-runs"
ORIGIN = {"Origin": os.environ.get("APP_ORIGIN", "http://127.0.0.1:8080")}
SCENARIO = {"scenario_key": "dco-basic", "scenario_version": "1.0"}
POSTGRES = pytest.mark.skipif(os.environ.get("RUN_POSTGRES_TESTS") != "1", reason="Requires isolated PostgreSQL")


def test_start_contract_requires_key_and_session():
    with TestClient(app) as client:
        assert client.get("/api/v1/simulation").status_code == 401
        assert client.post(BASE, headers={"Idempotency-Key": "start"}, json=SCENARIO).status_code == 401
        assert client.get("/health").json() == {"status": "ok"}


@pytest.fixture
def accounts():
    # The test database is disposable. Creating accounts directly avoids exercising
    # future education registration or touching the existing bootstrap account.
    user_names = [f"simulation-{uuid4()}" for _ in range(2)]
    password = "synthetic-test-password"
    hashed = password_hasher.hash(password)
    user_ids = [uuid4() for _ in user_names]
    owner_engine = create_engine(engine().url.set(username="postgres", password=os.environ["TEST_DB_OWNER_PASSWORD"]))
    try:
        with Session(owner_engine) as db:
            db.add_all([
                User(id=user_id, username=name, display_name=name, password_hash=hashed,
                     is_active=True, created_at=datetime.now(timezone.utc))
                for name, user_id in zip(user_names, user_ids, strict=True)
            ])
            db.commit()
    finally:
        owner_engine.dispose()
    return [(name, password, user_id) for name, user_id in zip(user_names, user_ids, strict=True)]


def login(client, account):
    username, password, _ = account
    response = client.post("/api/v1/auth/login", headers=ORIGIN, json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {**ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def start(client, auth, key="start", scenario=SCENARIO):
    return client.post(BASE, headers={**auth, "Idempotency-Key": key}, json=scenario)


@POSTGRES
def test_run_start_clone_and_access_boundaries(accounts):
    with TestClient(app) as alice, TestClient(app) as bob:
        alice_auth = login(alice, accounts[0])
        bob_auth = login(bob, accounts[1])
        assert alice.get("/api/v1/simulation").json()["status"] == "NOT_STARTED"
        assert start(alice, ORIGIN).status_code == 403
        assert alice.post(BASE, headers=alice_auth, json=SCENARIO).status_code == 422
        assert start(alice, alice_auth, "has space").status_code == 422
        assert start(alice, alice_auth, scenario={**SCENARIO, "scenario_version": "missing"}).status_code == 404

        created = start(alice, alice_auth)
        assert created.status_code == 201, created.text
        run = created.json()
        path = created.headers["Location"]
        assert alice.get(path).json() == run
        assert alice.get("/api/v1/simulation").json()["active_run_id"] == run["id"]
        assert run["virtual_now"] == "2026-11-20T08:00:00+09:00"
        assert run["shift"]["type"] == "DAY"
        assert run["shift"]["ends_at"] == "2026-11-20T20:00:00+09:00"
        slots = alice.get(f"{path}/schedule").json()["slots"]
        assert [slot["actor_type"] for slot in slots] == ["BOT", "TRAINEE", "BOT"]
        assert [slot["ends_at"] for slot in slots[:-1]] == [slot["starts_at"] for slot in slots[1:]]
        assert slots[2]["ends_at"] == "2026-11-21T08:00:00+09:00"

        retry = start(alice, alice_auth)
        assert retry.status_code == 200 and retry.json() == run
        assert start(alice, alice_auth, "another").json()["error"]["code"] == "SIMULATION_ALREADY_ACTIVE"
        assert start(alice, alice_auth, scenario={**SCENARIO, "scenario_version": "changed"}).status_code == 409

        rows = alice.get(f"{path}/tickets").json()["items"]
        hosts = alice.get(f"{path}/hosts").json()["items"]
        assert {row["training_origin"] for row in rows} == {"INITIAL", "CARRYOVER"}
        assert {row["scenario_host_id"] for row in rows} == {host["id"] for host in hosts}
        carryover = next(row for row in rows if row["training_origin"] == "CARRYOVER")
        assert carryover["detected_at"] == "2026-11-19T21:20:00Z"
        link, = alice.get(f"{path}/incoming-handover").json()["tickets"]
        assert link["ticket_id"] == carryover["id"]
        assert alice.get(link["ticket_url"]).json() == carryover

        for suffix in ("", "/schedule", "/hosts", "/incoming-handover", "/tickets"):
            assert bob.get(path + suffix).status_code == 404
        assert bob.get(link["ticket_url"]).status_code == 404
        bob_start = start(bob, bob_auth)
        assert bob_start.status_code == 201, bob_start.text
        bob_path = bob_start.headers["Location"]
        assert bob.get(f"{bob_path}/tickets/{carryover['id']}").status_code == 404
        bob_rows = bob.get(f"{bob_path}/tickets").json()["items"]
        assert {row["id"] for row in bob_rows}.isdisjoint(row["id"] for row in rows)
        assert {row["detected_at"] for row in bob_rows} == {row["detected_at"] for row in rows}

        legacy = f"/api/v1/tickets/{carryover['id']}"
        assert alice.get(legacy).status_code == 404
        assert alice.get(f"{legacy}/operations").status_code == 404
        assert alice.get("/api/v1/tickets", params={"q": carryover["number"]}).json()["items"] == []
        assert alice.post(f"/api/v1/knowledge/tickets/{carryover['id']}/reindex", headers=alice_auth).status_code == 404
        assert alice.post("/api/v1/agent/analyze", headers=alice_auth, json={
            "ticket_id": carryover["id"], "message": "스토리지 장애를 확인하세요.",
        }).status_code == 404


@POSTGRES
@pytest.mark.parametrize("keys, expected", [(("one", "two"), [201, 409]), (("same", "same"), [200, 201])])
def test_concurrent_start(accounts, keys, expected):
    with TestClient(app) as client:
        auth = login(client, accounts[0])

        def submit(key):
            with TestClient(app, cookies=client.cookies) as contender:
                return start(contender, auth, key).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(submit, keys)) == expected
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(SimulationRun).where(
            SimulationRun.user_id == accounts[0][2],
        )) == 1


@POSTGRES
def test_switch_preserves_work_and_resumes_same_run(accounts):
    with TestClient(app) as alice, TestClient(app) as bob:
        auth = login(alice, accounts[0])
        bob_auth = login(bob, accounts[1])
        first = start(alice, auth)
        assert first.status_code == 201, first.text
        former_id, former_path = first.json()["id"], first.headers["Location"]
        ticket, = alice.get(former_path + "/incoming-handover").json()["tickets"]
        action = {"action_code": "CUSTOM_NOTE", "note": "저장한 장애 관측 메모"}
        note = alice.post(ticket["ticket_url"] + "/actions", headers={**auth, "Idempotency-Key": "note"}, json=action)
        assert note.status_code == 201, note.text
        outgoing = former_path + "/outgoing-handover"
        draft = alice.put(outgoing, headers=auth, json={
            "expected_revision": 0, "content": {"summary": "저장한 인계 초안"},
        })
        assert draft.status_code == 200, draft.text

        selection = {**SCENARIO, "scenario_version": "1.1", "pause_active_run_id": former_id}
        stale = start(alice, auth, "switch", {**selection, "pause_active_run_id": str(uuid4())})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "SIMULATION_ACTIVE_CHANGED"
        assert start(alice, auth, "switch", {**selection, "scenario_version": "missing"}).status_code == 404
        assert alice.get(former_path).json()["status"] == "ACTIVE"
        switched = start(alice, auth, "switch", selection)
        assert switched.status_code == 201, switched.text
        new_id, new_path = switched.json()["id"], switched.headers["Location"]
        assert new_id != former_id
        assert start(alice, auth, "switch", selection).json() == switched.json()
        assert start(alice, auth, "switch", {**selection, "pause_active_run_id": new_id}).status_code == 409

        assert alice.get(former_path).json()["status"] == "PAUSED"
        assert alice.get(former_path).json()["completed_at"] is None
        assert alice.get(outgoing).json() == draft.json()
        assert alice.get(ticket["ticket_url"] + "/actions").json()["activity"] == [note.json()]
        rejected_note = alice.post(ticket["ticket_url"] + "/actions", headers={**auth, "Idempotency-Key": "paused"}, json=action)
        assert rejected_note.status_code == 409 and rejected_note.json()["error"]["code"] == "SIMULATION_PAUSED"
        final_body = {"expected_revision": draft.json()["revision"], "confirm": True}
        final_headers = {**auth, "Idempotency-Key": "finalize"}
        finalize = alice.post(outgoing + "/finalize", headers=final_headers, json=final_body)
        assert finalize.status_code == 409 and finalize.json()["error"]["code"] == "SIMULATION_PAUSED"

        resume_path = former_path + "/resume"
        resume_body = {"pause_active_run_id": new_id}
        assert bob.post(resume_path, headers=bob_auth, json=resume_body).status_code == 404
        assert alice.post(resume_path, headers=auth, json={}).status_code == 409
        resumed = alice.post(resume_path, headers=auth, json=resume_body)
        assert resumed.status_code == 200 and resumed.json() == first.json()
        assert alice.post(resume_path, headers=auth, json=resume_body).json() == resumed.json()
        assert alice.get(new_path).json()["status"] == "PAUSED"
        assert alice.get("/api/v1/simulation").json()["active_run_id"] == former_id
        assert alice.post(outgoing + "/finalize", headers=final_headers, json=final_body).status_code == 200
        assert alice.post(resume_path, headers=auth, json={}).status_code == 409
        assert alice.post(new_path + "/resume", headers=auth, json={}).json()["status"] == "ACTIVE"


@POSTGRES
def test_concurrent_switches_keep_one_active_run(accounts):
    with TestClient(app) as client:
        auth = login(client, accounts[0])
        active = start(client, auth).json()["id"]

        def switch(key):
            with TestClient(app, cookies=client.cookies) as contender:
                return start(contender, auth, key, {**SCENARIO, "pause_active_run_id": active}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(switch, ("switch-1", "switch-2"))) == [201, 409]
        assert sorted(row["status"] for row in client.get(BASE).json()["items"]) == ["ACTIVE", "PAUSED"]


@POSTGRES
def test_seed_failure_rolls_back_and_key_can_be_retried(accounts, monkeypatch):
    original = routes.record_event

    def fail(*args):
        raise RuntimeError("test failure after inserting tickets and hosts")

    with TestClient(app, raise_server_exceptions=False) as client:
        auth = login(client, accounts[0])
        monkeypatch.setattr(routes, "record_event", fail)
        assert start(client, auth).status_code == 500
        with Session(engine()) as db:
            assert db.scalar(select(SimulationRun.id).where(SimulationRun.user_id == accounts[0][2])) is None
            assert db.scalar(select(Ticket.id).where(Ticket.created_by == accounts[0][2])) is None

        monkeypatch.setattr(routes, "record_event", original)
        response = start(client, auth)
        assert response.status_code == 201, response.text
        selection = {**SCENARIO, "pause_active_run_id": response.json()["id"]}
        monkeypatch.setattr(routes, "record_event", fail)
        assert start(client, auth, "failed-switch", selection).status_code == 500
        assert client.get(BASE).json()["items"] == [response.json()]
        with Session(engine()) as db:
            run_id = UUID(response.json()["id"])
            assert db.scalar(select(func.count()).select_from(SimulationRunHost).where(
                SimulationRunHost.simulation_run_id == run_id,
            )) == 2
            run = db.get(SimulationRun, run_id)
            db.add(SimulationRun(user_id=run.user_id, scenario_id=run.scenario_id, status="ACTIVE",
                                 started_at=run.started_at, incoming_report_snapshot={}, start_request_key="bypass"))
            with pytest.raises(IntegrityError):
                db.flush()
