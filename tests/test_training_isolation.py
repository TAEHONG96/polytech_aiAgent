import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app import auth as auth_module
from app.agent.models import AgentRun
from app.agent.provider import FakeGenerator
from app.agent.queue import process_agent_run
from app.agent.tools import Toolbox
from app.db import engine
from app.knowledge import routes as knowledge
from app.knowledge.models import IndexJob, KnowledgeChunk, SopDocument
from app.models import Ticket, TicketEvent
from app.settings import settings
from app.simulation.models import ScenarioIncident, SimulationEvaluation, SimulationRun, SimulationTicketEvaluation
from main import app
from test_simulation import ORIGIN, POSTGRES, start
from test_tickets import PASSWORD, USER, ticket_input

pytestmark = POSTGRES
REGISTER = "/api/v1/auth/register"
PASS = "synthetic-training-password"


def request(who, method, url, status, **kwargs):
    response = who.request(method, url, **kwargs)
    assert response.status_code == status, response.text
    return response


def logged_in(who, username, password, role):
    response = request(who, "POST", "/api/v1/auth/login", 200, headers=ORIGIN,
                       json={"username": username, "password": password})
    assert response.json()["roles"] == [role]
    assert "HttpOnly" in response.headers["Set-Cookie"] and "SameSite=lax" in response.headers["Set-Cookie"]
    assert request(who, "GET", "/api/v1/auth/session", 200).json()["roles"] == [role]
    return {**ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


@pytest.fixture
def data_owner():
    owner = create_engine(engine().url.set(username="postgres", password=os.environ["TEST_DB_OWNER_PASSWORD"]))
    try:
        yield owner
    finally:
        owner.dispose()


@pytest.fixture
def trainees(monkeypatch):
    monkeypatch.setattr(auth_module, "settings", lambda: replace(settings(), training_registration=True))
    with TestClient(app) as alice, TestClient(app) as bob:
        actors = []
        for who in (alice, bob):
            name = f"Trainee-{uuid4().hex}"
            response = request(who, "POST", REGISTER, 201, headers=ORIGIN, json={"username": name, "password": PASS})
            assert "Set-Cookie" not in response.headers
            request(who, "GET", "/api/v1/auth/session", 401)
            actor_id = UUID(response.json()["id"])
            headers = logged_in(who, name.upper(), PASS, "trainee")
            launched = start(who, headers)
            assert launched.status_code == 201, launched.text
            actors.append((who, headers, launched.headers["Location"], actor_id))
        yield actors


def first_ticket(who, run):
    return request(who, "GET", run + "/tickets", 200).json()["items"][0]


def finish(owner, run_path):
    with Session(owner) as db, db.begin():
        run = db.get(SimulationRun, UUID(run_path.rsplit("/", 1)[1]))
        run.status = "COMPLETED"
        run.completed_at = datetime.now(timezone.utc)


def test_registration_privilege_cookie_and_safe_default(monkeypatch):
    with monkeypatch.context() as config:
        config.setenv("APP_ENV", "development")
        config.setenv("APP_TRAINING_REGISTRATION", "1")
        config.setenv("APP_ORIGIN", "https://public.example")
        assert not settings.__wrapped__().training_registration
        config.setenv("APP_ORIGIN", "http://127.0.0.1:18159")
        assert settings.__wrapped__().training_registration
    name = f"Trainee-{uuid4().hex}"
    body = {"username": name, "password": PASS}
    with TestClient(app) as who:
        capability = request(who, "GET", "/api/v1/auth/registration", 200)
        assert capability.json() == {"enabled": False} and capability.headers["Cache-Control"] == "no-store"
        request(who, "POST", REGISTER, 404, headers=ORIGIN, json=body)
        monkeypatch.setattr(auth_module, "settings", lambda: replace(settings(), training_registration=True))
        assert request(who, "GET", "/api/v1/auth/registration", 200).json() == {"enabled": True}
        request(who, "POST", REGISTER, 403, headers={"Origin": "https://invalid.example"}, json=body)
        request(who, "POST", REGISTER, 422, headers=ORIGIN, json={**body, "password": "short"})
        request(who, "POST", REGISTER, 422, headers=ORIGIN, json={**body, "role": "operator"})
        request(who, "POST", REGISTER, 201, headers=ORIGIN, json=body)
        request(who, "POST", REGISTER, 409, headers=ORIGIN, json={**body, "username": name.upper()})
        request(who, "POST", REGISTER, 409, headers=ORIGIN, json={**body, "username": USER})
        authenticated = logged_in(who, name.lower(), PASS, "trainee")
        request(who, "POST", REGISTER, 409, headers=ORIGIN, json={**body, "username": name + "another"})
        for endpoint in ("/tickets", "/sops", "/handovers", "/handover-operators", "/shift-instances",
                         f"/agent/runs/{uuid4()}"):
            request(who, "GET", "/api/v1" + endpoint, 403)
        request(who, "POST", "/api/v1/knowledge/search", 403, headers=authenticated, json={"query": "synthetic"})
        request(who, "POST", "/api/v1/auth/logout", 403, headers=ORIGIN)
        request(who, "POST", "/api/v1/auth/logout", 204, headers=authenticated)
        request(who, "GET", "/api/v1/auth/session", 401)
    with engine().connect() as db:
        assert db.scalar(text("SELECT NOT has_table_privilege('users', 'INSERT')"))


def test_direct_ids_writes_revisions_and_completed_execution(trainees, data_owner):
    (alice, alice_auth, a, _), (bob, bob_auth, b, bob_id) = trainees
    ta, tb = first_ticket(alice, a), first_ticket(bob, b)
    ta_url, tb_url = f"{a}/tickets/{ta['id']}", f"{b}/tickets/{tb['id']}"
    request(alice, "GET", tb_url, 404)
    request(alice, "GET", f"{a}/tickets/{tb['id']}", 404)
    request(alice, "PATCH", ta_url, 403, headers=ORIGIN, json={"expected_version": ta["version"], "title": "bypass"})
    changed = request(alice, "PATCH", ta_url, 200, headers=alice_auth,
                      json={"expected_version": ta["version"], "title": "합성 Alice 티켓"}).json()
    assert request(bob, "GET", tb_url, 200).json() == tb
    work = {"expected_version": tb["version"], "title": "합성 작업", "status": "todo"}
    foreign_work = request(bob, "POST", tb_url + "/work", 201, headers=bob_auth, json=work).json()["work"][0]
    work["expected_version"] = changed["version"]
    request(alice, "PUT", ta_url + "/work/" + foreign_work["id"], 404, headers=alice_auth, json=work)
    request(alice, "POST", ta_url + "/work", 404, headers=alice_auth, json={**work, "owner_id": str(bob_id)})
    request(alice, "POST", ta_url + "/work", 201, headers=alice_auth, json=work)
    create = {**ticket_input("합성 사용자 생성"), "scenario_host_id": ta["scenario_host_id"]}
    created = request(alice, "POST", a + "/tickets", 201, headers=alice_auth, json=create)
    assert created.json()["training_origin"] == "USER_CREATED"
    request(alice, "GET", created.headers["Location"], 200)

    draft_path = a + "/outgoing-handover"
    edit = {"expected_revision": 0, "selection": {"include_ticket_ids": [ta["id"]]},
            "content": {"summary": "합성 요약", "open_items": [{"ticket_id": ta["id"], "next_action": "합성 확인"}]}}
    request(alice, "PUT", draft_path, 404, headers=alice_auth,
            json={**edit, "selection": {"include_ticket_ids": [tb["id"]]}})
    draft = request(alice, "PUT", draft_path, 200, headers=alice_auth, json=edit).json()
    assert draft["revision"] == 1 and ta["id"] in {item["id"] for item in draft["items"]}
    request(bob, "GET", draft_path + "/revisions/1", 404)
    request(alice, "GET", b + "/outgoing-handover/revisions/1", 404)
    with TestClient(app) as operator:
        logged_in(operator, USER, PASSWORD, "operator")
        request(operator, "GET", "/api/v1/handovers/" + draft["handover_id"], 404)
        request(operator, "GET", "/api/v1/tickets/" + ta["id"], 404)
    finish(data_owner, a)
    assert start(alice, alice_auth, "second").status_code == 201
    a2 = request(alice, "GET", "/api/v1/simulation", 200).json()["active_run_id"]
    current = f"/api/v1/simulation-runs/{a2}"
    request(alice, "GET", current + "/tickets/" + ta["id"], 404)
    request(alice, "GET", current + "/outgoing-handover/revisions/1", 404)
    request(alice, "GET", draft_path + "/revisions/1", 200)
    request(alice, "GET", ta_url + "/operations", 200)
    for method, url, body in (
        ("PATCH", ta_url, {"expected_version": 3, "title": "denied"}),
        ("POST", ta_url + "/work", work),
        ("POST", ta_url + "/analyses", {"message": "denied"}),
        ("PUT", draft_path, {"expected_revision": 1}),
        ("POST", draft_path + "/generate", {"expected_revision": 1}),
    ):
        assert request(alice, method, url, 409, headers=alice_auth, json=body).json()["error"]["code"] == "SIMULATION_COMPLETED"


def test_sql_candidates_and_originals_are_authorized_before_limit(trainees, data_owner, monkeypatch):
    (alice, headers, a, alice_id), (bob, _, b, _) = trainees
    ta, tb = first_ticket(alice, a), first_ticket(bob, b)
    profile = f"qa-training-{uuid4().hex}"
    high, lower = [1.0, 0.0] + [0.0] * 766, [0.8, 0.6] + [0.0] * 766
    monkeypatch.setattr(knowledge, "worker_query_vector", lambda query, embedding_profile: high)
    monkeypatch.setattr(knowledge, "embedding_profile", lambda: SimpleNamespace(fingerprint=profile))
    with TestClient(app) as operator:
        op = logged_in(operator, USER, PASSWORD, "operator")
        sops = []
        for label in ("old", "approved"):
            sop = request(operator, "POST", "/api/v1/sops", 201, headers=op, files={
                "file": (f"{label}.txt", f"{label} synthetic {uuid4()}".encode(), "text/plain"),
            }).json()
            sops.append(UUID(sop["id"]))
    now = datetime.now(timezone.utc)
    with Session(data_owner) as db, db.begin():
        for uid, embedding in zip(sops, (high, lower), strict=True):
            db.get(SopDocument, uid).training_approved = uid == sops[1]
            job = db.get(IndexJob, uid)
            job.status, job.indexed_profile = "COMPLETED", profile
            db.add(KnowledgeChunk(source_id=uid, chunk_index=0, content="synthetic SOP", embedding=embedding,
                                  embedding_model="fake", embedding_profile=profile, created_at=now))
        for row, vector in ((ta, lower), (tb, high)):
            uid = UUID(row["id"])
            ticket = db.get(Ticket, uid)
            ticket.status, ticket.resolution, ticket.resolved_at = "resolved", "합성 해결", now
            db.add(IndexJob(id=uid, source_type="TICKET", ticket_id=uid, ticket_version=ticket.version,
                            status="COMPLETED", attempts=0, available_at=now, updated_at=now, indexed_profile=profile))
            db.flush()
            db.add(KnowledgeChunk(source_id=uid, chunk_index=0, content="synthetic case", embedding=vector,
                                  embedding_model="fake", embedding_profile=profile, created_at=now))
        target = db.get(Ticket, UUID(ta["id"]))
        incidents = [uuid4() for _ in range(4)]
        for uid, delta, approved, vector in zip(incidents, (-1, 0, 1, -1), (True, True, True, False),
                                               (lower, high, high, high), strict=True):
            db.add(ScenarioIncident(id=uid, scenario_id=target.scenario_id, scenario_host_id=target.scenario_host_id,
                                    occurred_at=target.detected_at + timedelta(seconds=delta), approved=approved,
                                    title="합성 사고", resolution="합성 이력", embedding=vector, embedding_profile=profile))

    def search(source, ticket_id=None, status=200):
        payload = {"query": "synthetic ranking", "source_types": [source], "limit": 1}
        if ticket_id is not None:
            payload["ticket_id"] = ticket_id
        return request(alice, "POST", a + "/knowledge/search", status, headers=headers, json=payload).json()

    for source, target_id, expected in (("SOP", None, sops[1]), ("TICKET", None, ta["id"]),
                                         ("INCIDENT", ta["id"], incidents[0])):
        hit, = search(source, target_id)["results"]
        assert hit["source_id"] == str(expected)
        request(alice, "GET", hit["source_url"], 200)
        request(bob, "GET", hit["source_url"], 404)
    assert search("INCIDENT")["results"] == []
    search("INCIDENT", tb["id"], status=404)
    request(alice, "GET", a + f"/sops/{sops[0]}/original", 404)
    assert request(alice, "GET", a + f"/sops/{sops[1]}/original", 200).content.startswith(b"approved")
    listing = request(alice, "GET", a + f"/tickets/{ta['id']}/host-incidents", 200).json()["items"]
    assert str(incidents[0]) in {item["id"] for item in listing}
    for uid in incidents[1:]:
        request(alice, "GET", a + f"/tickets/{ta['id']}/incident-sources/{uid}", 404)
    outcome = asyncio.run(Toolbox(alice_id, UUID(tb["id"]), run_id=UUID(a.rsplit("/", 1)[1]))
                          .execute("get_ticket_details", {"ticket_id": tb["id"]}))
    assert outcome.status == "rejected" and "untrusted_data" not in outcome.payload
    with Session(data_owner) as db, db.begin():
        stored = AgentRun(id=uuid4(), requested_by=alice_id, simulation_run_id=UUID(a.rsplit("/", 1)[1]),
                          ticket_id=UUID(ta["id"]), message="synthetic", status="succeeded", created_at=now,
                          result={"summary": "approved before revocation", "evidence": [{
                              "kind": "SOP", "source_id": str(sops[1]), "source_url": a + f"/sops/{sops[1]}",
                          }]})
        db.add(stored)
        stored_id = stored.id
        analysis_path = a + f"/analyses/{stored_id}"
    request(alice, "GET", analysis_path, 200)
    with Session(data_owner) as db, db.begin():
        db.get(SopDocument, sops[1]).training_approved = False
    request(alice, "GET", analysis_path, 404)
    request(alice, "GET", a + f"/sops/{sops[1]}/original", 404)
    finish(data_owner, a)
    later = start(alice, headers, "search-second")
    assert later.status_code == 201
    new_path = later.headers["Location"]
    assert request(alice, "POST", new_path + "/knowledge/search", 200, headers=headers,
                   json={"query": "synthetic ranking", "source_types": ["TICKET"]}).json()["results"] == []
    request(alice, "GET", new_path + f"/analyses/{stored_id}", 404)
    assert search("TICKET")["results"][0]["source_id"] == ta["id"]


def test_worker_and_evaluation_parent_scope(trainees, data_owner):
    (alice, auth, a, alice_id), (bob, _, b, _) = trainees
    ta, tb = first_ticket(alice, a), first_ticket(bob, b)
    ta_url = a + "/tickets/" + ta["id"]
    request(alice, "POST", a + f"/tickets/{tb['id']}/analyses", 404, headers=auth, json={"message": "synthetic"})
    while process_agent_run(FakeGenerator):
        pass
    run_id = UUID(a.rsplit("/", 1)[1])
    queued = request(alice, "POST", ta_url + "/analyses", 202, headers=auth, json={"message": "합성 진단"})
    assert process_agent_run(FakeGenerator)
    location = queued.headers["Location"]
    result = request(alice, "GET", location, 200).json()
    assert result["status"] == "succeeded", result["error"]
    request(bob, "GET", location, 404)
    request(alice, "GET", b + "/analyses/" + result["analysis_id"], 404)
    for cite in result["result"]["evidence"]:
        request(alice, "GET", cite["source_url"], 200)
        request(bob, "GET", cite["source_url"], 404)

    with Session(data_owner) as db, db.begin():
        malformed = AgentRun(id=uuid4(), requested_by=alice_id, simulation_run_id=run_id, ticket_id=UUID(tb["id"]),
                             message="synthetic invalid parent", status="queued", created_at=datetime.now(timezone.utc))
        db.add(malformed)
        bad_id = malformed.id
    assert process_agent_run(lambda: pytest.fail("Unauthorized job called provider"))
    with Session(data_owner) as db:
        rejected = db.get(AgentRun, bad_id)
        assert rejected.error_code == "AI_UNAUTHORIZED" and rejected.result is None

    generate = request(alice, "POST", a + "/outgoing-handover/generate", 202, headers=auth,
                       json={"expected_revision": 0})
    assert process_agent_run(FakeGenerator)
    report_result = request(alice, "GET", generate.headers["Location"], 200).json()
    assert report_result["status"] == "succeeded", report_result["error"]
    assert all(item["source_url"].startswith(a + "/tickets/") for item in report_result["result"]["highlights"])
    draft = request(alice, "GET", a + "/outgoing-handover", 200).json()
    finish(data_owner, a)
    with Session(data_owner) as db, db.begin():
        created = datetime.now(timezone.utc)
        evaluation = SimulationEvaluation(id=uuid4(), simulation_run_id=run_id, handover_id=UUID(draft["handover_id"]),
                                          handover_revision=draft["revision"], rule_version="synthetic",
                                          input_snapshot={}, rules_status="READY", explanation_status="READY",
                                          created_at=created, updated_at=created)
        db.add(evaluation)
        db.flush()
        foreign_event = db.scalar(select(TicketEvent.id).where(TicketEvent.ticket_id == UUID(tb["id"])))
        for row in (ta, tb):
            db.add(SimulationTicketEvaluation(evaluation_id=evaluation.id, ticket_id=UUID(row["id"]),
                                              activity_status="NOT_ATTEMPTED", final_status=row["status"],
                                              rule_results=[{"rule_code": "PRIVATE", "result": "PASS",
                                                             "evidence_refs": [str(foreign_event)]}],
                                              evidence=[{"kind": "event", "id": str(foreign_event)}],
                                              explanation={"summary": "must be hidden"}))
    evaluation_url = a + "/evaluation"
    assert request(alice, "GET", evaluation_url, 200).json()["evaluated_ticket_counts"]["total"] == 1
    result = request(alice, "GET", evaluation_url + "/tickets/" + ta["id"], 200).json()
    assert result["rule_results"] == [] and result["feedback"] is None
    request(alice, "GET", evaluation_url + "/tickets/" + tb["id"], 404)
    request(bob, "GET", evaluation_url, 404)
