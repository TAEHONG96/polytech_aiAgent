from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from google.genai import types
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.agent.provider import AnalysisFailure, FakeGenerator, reply
from app.agent.queue import process_agent_run, utcnow
from app.db import engine
from app.simulation import evaluation_worker as worker
from app.simulation.models import SimulationEvaluation, SimulationRun, SimulationTicketEvaluation
from main import app
from test_simulation import POSTGRES, start
from test_simulation_workflows import SCENARIO, students, submit, tickets
from test_tickets import ticket_input
from test_training_isolation import data_owner, request

pytestmark = POSTGRES


def finalize(who, headers, run, status, *, revision=0, key="finalize", confirm=True):
    return request(who, "POST", run + "/outgoing-handover/finalize", status, json={
        "confirm": confirm, "expected_revision": revision,
    }, headers={**headers, "Idempotency-Key": key}).json()


def collect(who, headers, run, revision=0, **body):
    return request(who, "PUT", run + "/outgoing-handover", 200, headers=headers,
                   json={"expected_revision": revision, "refresh_snapshot": True, **body}).json()


def summary(who, run):
    return request(who, "GET", run + "/evaluation", 200).json()


def detail(who, run, ticket_id):
    return request(who, "GET", run + "/evaluation/tickets/" + ticket_id, 200).json()


def advance(who, run, stage, target="READY", factory=FakeGenerator):
    for _ in range(30):
        if summary(who, run)[stage + "_status"] == target:
            return
        assert worker.process(stage, factory)
    raise AssertionError(f"{stage} did not reach {target}")


def test_finalize_unresolved_freeze_scope_and_repractice(students):
    (alice, headers, run, _), (bob, _, other, _) = students
    seeded = tickets(alice, run)
    company = seeded["INITIAL"]
    target = run + "/tickets/" + company["id"]
    created = request(alice, "POST", run + "/tickets", 201, headers=headers, json={
        **ticket_input("합성 직접 등록"), "scenario_host_id": company["scenario_host_id"],
    }).json()
    outcome = finalize(alice, headers, run, 200)
    assert outcome["simulation_status"] == "COMPLETED" and outcome["report_status"] == "FINALIZED"
    assert outcome["evaluation"] == {"rules_status": "PENDING", "explanation_status": "NOT_STARTED"}
    assert {warning["code"] for warning in outcome["warnings"]} >= {"MISSING_SUMMARY", "MISSING_NEXT_ACTION"}
    assert finalize(alice, headers, run, 200) == outcome
    assert finalize(alice, headers, run, 409, revision=1)["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert finalize(alice, headers, run, 409, key="other")["error"]["code"] == "VERSION_CONFLICT"
    draft = request(alice, "GET", run + "/outgoing-handover", 200).json()
    assert draft["status"] == "FINALIZED" and draft["warnings"] == outcome["warnings"]
    assert summary(alice, run)["evaluated_ticket_counts"] == {"total": 2, "resolved": 0, "unresolved": 2}
    assert detail(alice, run, company["id"])["rule_results"] is None
    request(alice, "GET", run + "/evaluation/tickets/" + created["id"], 404)
    request(bob, "GET", run + "/evaluation", 404)
    request(bob, "GET", run + "/outgoing-handover/revisions/" + str(draft["revision"]), 404)
    request(bob, "GET", other + "/evaluation/tickets/" + company["id"], 409)
    submit(alice, headers, target, "CHECK_SERVER_STATUS", 409)
    request(alice, "POST", target + "/analyses", 409, headers=headers, json={"message": "synthetic"})
    request(alice, "PATCH", target, 409, headers=headers, json={"expected_version": company["version"], "title": "denied"})
    request(alice, "POST", target + "/work", 409, headers=headers, json={
        "expected_version": company["version"], "title": "denied", "status": "todo",
    })
    request(alice, "PUT", run + "/outgoing-handover", 409, headers=headers, json={"expected_revision": draft["revision"]})
    next_run = start(alice, headers, "next-run", scenario=SCENARIO)
    assert next_run.status_code == 201 and next_run.headers["Location"] != run
    request(alice, "GET", next_run.headers["Location"] + "/tickets/" + company["id"], 404)
    assert detail(alice, run, company["id"])["activity_status"] == "NOT_ATTEMPTED"
    finalize(alice, headers, next_run.headers["Location"], 200)
    request(alice, "GET", next_run.headers["Location"] + "/evaluation/tickets/" + company["id"], 404)


def test_structure_source_and_report_version_conflicts(students):
    (alice, headers, run, _), (bob, _, foreign, _) = students
    own = tickets(alice, run)["INITIAL"]
    elsewhere = tickets(bob, foreign)["INITIAL"]["id"]
    finalize(alice, headers, run, 422, confirm=False)
    draft = collect(alice, headers, run)
    rev = draft["revision"]
    request(alice, "PUT", run + "/outgoing-handover", 422, headers=headers, json={
        "expected_revision": rev, "content": {"open_items": [{"ticket_id": elsewhere, "next_action": "synthetic"}]},
    })
    finalize(alice, headers, run, 409, revision=rev - 1)
    submit(alice, headers, run + "/tickets/" + own["id"], "CHECK_SERVER_STATUS", 201)
    assert finalize(alice, headers, run, 409, revision=rev)["error"]["code"] == "REPORT_SOURCE_CHANGED"
    refreshed = collect(alice, headers, run, rev)
    finalize(alice, headers, run, 200, revision=refreshed["revision"])


def test_concurrent_finalize_and_replay(students, data_owner):
    (alice, headers, run, _), _ = students
    barrier = Barrier(2)

    def submit_key(key):
        with TestClient(app, cookies=alice.cookies) as client:
            barrier.wait(timeout=10)
            response = client.post(run + "/outgoing-handover/finalize",
                                   headers={**headers, "Idempotency-Key": key},
                                   json={"confirm": True, "expected_revision": 0})
            return key, response

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(submit_key, ["first", "second"]))
    assert sorted(response.status_code for _, response in replies) == [200, 409]
    key, original = next((key, response) for key, response in replies if response.status_code == 200)
    assert finalize(alice, headers, run, 200, key=key) == original.json()
    run_id = UUID(run.rsplit("/", 1)[-1])
    with Session(data_owner) as db:
        assert db.scalar(select(func.count()).select_from(SimulationEvaluation)
                         .where(SimulationEvaluation.simulation_run_id == run_id)) == 1


def test_action_and_finalization_share_run_lock(students):
    (alice, headers, run, _), _ = students
    target = run + "/tickets/" + tickets(alice, run)["INITIAL"]["id"]
    rev = collect(alice, headers, run)["revision"]
    barrier = Barrier(2)

    def post(kind):
        with TestClient(app, cookies=alice.cookies) as client:
            barrier.wait(timeout=10)
            path, body = (target + "/actions", {"action_code": "CHECK_SERVER_STATUS"}) if kind == "action" else (
                run + "/outgoing-handover/finalize", {"confirm": True, "expected_revision": rev})
            return client.post(path, headers={**headers, "Idempotency-Key": kind}, json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        action, ended = pool.map(post, ["action", "end"])
    assert (action.status_code, ended.status_code) in {(409, 200), (201, 409)}
    if action.status_code == 201:
        assert ended.json()["error"]["code"] == "REPORT_SOURCE_CHANGED"
        finalized = collect(alice, headers, run, rev)
        finalize(alice, headers, run, 200, revision=finalized["revision"])
        assert summary(alice, run)["last_event_sequence"] == action.json()["event_sequence"]
    else:
        assert request(alice, "GET", target + "/actions", 200).json()["activity"] == []


def test_failure_rolls_back_final_revision_and_reservation(students):
    (alice, headers, run, _), _ = students
    revision = collect(alice, headers, run)["revision"]

    def inject(_connection, _cursor, statement, *_):
        if statement.startswith("INSERT INTO simulation_ticket_evaluations"):
            raise RuntimeError("isolated finalization rollback")

    event.listen(engine(), "before_cursor_execute", inject)
    try:
        with TestClient(app, cookies=alice.cookies, raise_server_exceptions=False) as client:
            request(client, "POST", run + "/outgoing-handover/finalize", 500,
                    headers={**headers, "Idempotency-Key": "finalize"},
                    json={"confirm": True, "expected_revision": revision})
    finally:
        event.remove(engine(), "before_cursor_execute", inject)
    assert request(alice, "GET", run, 200).json()["status"] == "ACTIVE"
    assert request(alice, "GET", run + "/outgoing-handover", 200).json()["revision"] == revision
    request(alice, "GET", run + "/evaluation", 409)
    finalize(alice, headers, run, 200, revision=revision)


def test_real_actions_and_learning_evidence_rule_results(students):
    (alice, headers, run, _), _ = students
    pair = tickets(alice, run)
    storage = pair["CARRYOVER"]
    path = run + "/tickets/" + storage["id"]
    incident, = request(alice, "GET", path + "/host-incidents", 200).json()["incidents"]
    historical = request(alice, "GET", incident["source_url"], 200).json()
    assert historical["ticket_key"] == "INC-HIST-000011"
    assert historical["service"] == "storage" and historical["asset_kind"] == "storage"
    assert historical["host"]["host_key"] == "ASSET-001" and historical["error_code"] == "STORAGE_LATENCY"
    evidence = {"kind": "HOST_HISTORY", "activity": "VIEWED", "source_id": incident["id"]}
    options_path = path + "/learning-evidence/actions"
    params = {"kind": evidence["kind"], "source_id": evidence["source_id"]}
    early = submit(alice, headers, path, "CHECK_SERVER_STATUS", 201)
    assert request(alice, "GET", options_path, 200, params=params).json() == {"viewed": False, "linked": False, "items": []}
    request(alice, "POST", path + "/learning-evidence", 201, headers=headers, json=evidence)
    memo = submit(alice, headers, path, "CUSTOM_NOTE", 201, note="교체했다는 자유 기록")
    request(alice, "POST", path + "/learning-evidence", 422, headers=headers, json={
        **evidence, "activity": "USED", "action_event_id": memo["action_id"],
    })
    action = submit(alice, headers, path, "RUN_BMC_DIAGNOSTICS", 201, params={"target": "storage"})
    options = request(alice, "GET", options_path, 200, params=params).json()
    assert options["viewed"] and not options["linked"]
    assert [item["action_id"] for item in options["items"]] == [action["action_id"]]
    request(alice, "POST", path + "/learning-evidence", 422, headers=headers, json={
        **evidence, "activity": "USED", "action_event_id": early["action_id"],
    })
    request(alice, "POST", path + "/learning-evidence", 201, headers=headers, json={
        **evidence, "activity": "USED", "action_event_id": action["action_id"],
    })
    assert request(alice, "GET", options_path, 200, params=params).json() == {"viewed": True, "linked": True, "items": []}
    submit(alice, headers, path, "CHECK_STORAGE", 201)
    submit(alice, headers, path, "RERUN_DIAGNOSTICS", 201)
    own_memory = run + "/tickets/" + pair["INITIAL"]["id"]
    submit(alice, headers, own_memory, "CUSTOM_NOTE", 201, note="메모리를 교체했다는 자유 기록")
    draft = collect(alice, headers, run, content={
        "summary": "스토리지 재검증", "open_items": [{"ticket_id": storage["id"], "next_action": "지연 재확인"}],
    })
    finalize(alice, headers, run, 200, revision=draft["revision"])
    advance(alice, run, "rules")
    found = detail(alice, run, storage["id"])
    verdicts = {rule["rule_code"]: rule["result"] for rule in found["rule_results"]}
    assert found["final_status"] == "in_progress"
    for key in ("USE_HOST_HISTORY", "APPROPRIATE_VIRTUAL_ACTION", "POST_REPAIR_VALIDATION", "HANDOVER_UNRESOLVED"):
        assert verdicts[key] == "PASS"
    memo_only = detail(alice, run, pair["INITIAL"]["id"])
    assert memo_only["activity_status"] == "NOT_ATTEMPTED"
    assert not any(rule["result"] == "PASS" for rule in memo_only["rule_results"])
    for ref in found["evidence"]:
        request(alice, "GET", ref["source_url"], 200)


def test_archived_seed_ticket_is_still_evaluated(students):
    (alice, headers, run, _), _ = students
    initial = tickets(alice, run)["INITIAL"]
    target = run + "/tickets/" + initial["id"]
    version = initial["version"]
    for state in ("in_progress", "resolved", "closed"):
        body = {"expected_version": version, "to_status": state, "reason": "합성 상태 검증"}
        if state == "resolved":
            body["resolution"] = "합성 분석 결과"
        version = request(alice, "POST", target + "/transitions", 200, headers=headers, json=body).json()["version"]
    request(alice, "DELETE", target, 204, headers=headers, params={"expected_version": version})
    finalize(alice, headers, run, 200)
    assert summary(alice, run)["evaluated_ticket_counts"] == {"total": 2, "resolved": 1, "unresolved": 1}
    advance(alice, run, "rules")
    info = detail(alice, run, initial["id"])
    assert info["final_status"] == "closed"
    assert any(rule["rule_code"] == "APPROPRIATE_VIRTUAL_ACTION" and rule["result"] == "NOT_OBSERVED"
               for rule in info["rule_results"])


def test_impact_warning_and_resolved_ticket_handover_selection(students):
    (alice, headers, run, _), _ = students
    seeded = tickets(alice, run)
    initial = seeded["INITIAL"]
    target = run + "/tickets/" + initial["id"]
    carryover = seeded["CARRYOVER"]
    work = request(alice, "POST", run + "/tickets/" + carryover["id"] + "/work", 201, headers=headers, json={
        "expected_version": carryover["version"], "title": "스토리지 지연 재확인", "status": "todo",
        "owner_id": None,
    }).json()["work"][0]
    assert work["owner_id"] is None
    draft = collect(alice, headers, run)
    assert "WORK_OWNER_MISSING" not in {warning["code"] for warning in draft["warnings"]}
    carryover_item = next(item for item in draft["items"] if item["id"] == carryover["id"])
    assert work["id"] in {task["id"] for task in carryover_item["work"]}
    assert "work_owner" not in {gap["field"] for gap in carryover_item["gaps"]}

    def missing_impact(report):
        return any(warning["ticket_id"] == initial["id"] for warning in report["warnings"]
                   if warning["code"] == "MISSING_IMPACT")

    assert missing_impact(draft)
    updated = request(alice, "PATCH", target, 200, headers=headers, json={
        "expected_version": initial["version"], "impact": "합성 컴퓨팅 서비스 영향",
    }).json()
    draft = collect(alice, headers, run, draft["revision"])
    assert not missing_impact(draft)
    for state in ("in_progress", "resolved"):
        body = {"expected_version": updated["version"], "to_status": state, "reason": "합성 상태 검증"}
        if state == "resolved":
            body["resolution"] = "합성 분석 결과"
        updated = request(alice, "POST", target + "/transitions", 200, headers=headers, json=body).json()
    draft = collect(alice, headers, run, draft["revision"])
    assert initial["id"] not in {item["id"] for item in draft["items"]}
    finalize(alice, headers, run, 200, revision=draft["revision"])
    assert detail(alice, run, initial["id"])["final_status"] == "resolved"


def test_worker_expired_lease_stale_publisher_and_explanation_retry(students, data_owner):
    (alice, headers, run, _), _ = students
    while worker.process("rules"):
        pass
    while worker.process("explanation"):
        pass
    finalize(alice, headers, run, 200)
    old = worker.claim("rules")
    assert old.evaluation_id == UUID(summary(alice, run)["evaluation_id"])
    with Session(data_owner) as db, db.begin():
        db.get(SimulationEvaluation, old.evaluation_id).rules_lease_until = utcnow() - timedelta(seconds=1)
    assert worker.process("rules")
    assert worker.publish(old, {}) is False
    target = tickets(alice, run)["INITIAL"]["id"]
    before = detail(alice, run, target)
    assert before["rules_status"] == "READY" and before["explanation_status"] == "PENDING"
    assert before["feedback"] is None

    def unavailable():
        raise AnalysisFailure("AI_CONFIGURATION", "isolated synthetic provider failure")

    advance(alice, run, "explanation", "FAILED", unavailable)
    failed = detail(alice, run, target)
    assert before["rule_results"] == failed["rule_results"] and before["evidence"] == failed["evidence"]
    assert worker.retry_explanation(old.evaluation_id)
    advance(alice, run, "explanation")
    latest = detail(alice, run, target)
    assert latest["rule_results"] == before["rule_results"]
    assert latest["feedback"]["recommendations"]
    assert not worker.retry_explanation(old.evaluation_id)


def test_late_analysis_and_rejection_of_invented_feedback(students, data_owner):
    (alice, headers, run, _), _ = students
    while process_agent_run(FakeGenerator):
        pass
    while worker.process("rules"):
        pass
    while worker.process("explanation"):
        pass
    ticket_id = tickets(alice, run)["INITIAL"]["id"]
    started = request(alice, "POST", run + f"/tickets/{ticket_id}/analyses", 202, headers=headers,
                      json={"message": "합성 메모리 장애"}).headers["Location"]
    finalize(alice, headers, run, 200, key=uuid4().hex)
    eval_id = UUID(summary(alice, run)["evaluation_id"])
    with Session(data_owner) as db:
        frozen = db.get(SimulationEvaluation, eval_id).input_snapshot
    assert process_agent_run(FakeGenerator)
    assert request(alice, "GET", started, 200).json()["status"] == "succeeded"
    advance(alice, run, "rules")
    assert worker.claim("explanation").evaluation_id == eval_id
    with Session(data_owner) as db, db.begin():
        row = db.get(SimulationEvaluation, eval_id)
        row.explanation_lease_until = utcnow() - timedelta(seconds=1)
        row.explanation_attempts = worker.MAX_ATTEMPTS - 1

    class Invented(FakeGenerator):
        async def generate(self, contents, config):
            return reply(types.Part(text='{"focus":"INVENTED_ACTION","strengths":[],"improvements":[],"recommendations":[]}'))

    advance(alice, run, "explanation", "FAILED", Invented)
    with Session(data_owner) as db:
        assert db.get(SimulationEvaluation, eval_id).input_snapshot == frozen
        assert db.scalar(select(func.count()).select_from(SimulationTicketEvaluation)
                         .where(SimulationTicketEvaluation.evaluation_id == eval_id)) == 2
