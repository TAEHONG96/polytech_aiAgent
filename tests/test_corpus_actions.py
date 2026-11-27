import asyncio
import os
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.agent.tools import Toolbox
from app.db import engine
from app.settings import settings
from app.simulation.bmc_actions import BMC_SCENARIO, BMC_VERSION
from app.simulation.evaluation_rules import grade
from app.simulation.network_actions import NETWORK_SCENARIO, NETWORK_VERSION
from main import app
from tests import corpus_bmc
from tests.corpus_network import SOP_QUERY, SOP_SECTION


@pytest.mark.parametrize(("asset", "measurement", "other_measurement"), [
    ("network", "MEASURE_PACKET_LOSS", "MEASURE_STORAGE_LATENCY"),
    ("storage-path", "MEASURE_STORAGE_LATENCY", "MEASURE_PACKET_LOSS"),
])
def test_path_switch_evaluation_requires_matching_remeasurement(asset, measurement, other_measurement):
    def verdict(*actions):
        ticket = {
            "asset_kind": asset, "status": "in_progress", "included": True,
            "work": [], "resolution": None, "handover_next_action": "재측정 결과 인계",
            "events": [{"id": str(i), "sequence": i, "type": "virtual_action_performed", "details": {"action_code": code}}
                       for i, code in enumerate(actions, 1)],
        }
        return next(rule for rule in grade(ticket, "report", flexible=True)["rules"]
                    if rule["rule_code"] == "POST_REPAIR_VALIDATION")

    assert verdict(measurement, "SWITCH_REDUNDANT_PATH", "RERUN_DIAGNOSTICS", other_measurement)["result"] == "NOT_MET"
    matched = verdict("SWITCH_REDUNDANT_PATH", measurement)
    assert matched["result"] == "PASS" and matched["evidence_refs"] == ["1", "2"]


@pytest.mark.skipif(engine().url.database != "polytech_training_corpus" or os.getenv("APP_ENV") != "simulation",
                    reason="Requires the isolated seeded corpus")
def test_existing_corpus_options_repeatability_and_guarded_switch():
    origin = {"Origin": settings().origin}
    account = {"username": f"action-qa-{uuid4().hex}", "password": uuid4().hex}

    with TestClient(app, base_url=settings().origin) as client:
        assert client.post("/api/v1/auth/register", json=account, headers=origin).status_code == 201
        login = client.post("/api/v1/auth/login", json=account, headers=origin)
        assert login.status_code == 200
        auth = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}

        def write(path, body):
            return client.post(path, headers={**auth, "Idempotency-Key": uuid4().hex}, json=body)

        def perform(path, code, status=201):
            response = write(path + "/actions", {"action_code": code})
            assert response.status_code == status, response.text
            return response.json()

        def catalog(path):
            response = client.get(path + "/actions")
            assert response.status_code == 200
            return {entry["action_code"]: entry for entry in response.json()["catalog"]}

        expected = {
            "boot": ("CHECK_POST_LOG", "CHECK_BOOT_ORDER", "CHECK_BOOT_VOLUME"),
            "memory": ("COMPARE_ECC_COUNTERS", "CHECK_PROCESS_MEMORY", "REPLACE_MEMORY"),
            "storage": ("MEASURE_STORAGE_LATENCY", "COMPARE_BATCH_HISTORY", "SWITCH_REDUNDANT_PATH"),
            "network": ("COMPARE_PORT_COUNTERS", "SWITCH_REDUNDANT_PATH", "MEASURE_PACKET_LOSS"),
            "power": ("CHECK_POWER_INPUTS", "CHECK_UPSTREAM_POWER", "REQUEST_FACILITIES_CHECK"),
            "bmc": ("CHECK_MANAGEMENT_NETWORK", "CHECK_HOST_SERVICE", "OPEN_ALTERNATE_CONSOLE"),
            "major": ("CHECK_SERVICE_IMPACT", "REQUEST_ADDITIONAL_METRICS", "RECORD_SHARED_TIMELINE"),
            "recovery": ("COMPARE_RECOVERY_METRICS", "CHECK_RECURRENCE", "START_RECOVERY_OBSERVATION", "RECORD_REMAINING_RISK"),
        }
        for scenario, codes in expected.items():
            result = write("/api/v1/simulation-runs", {"scenario_key": f"synthetic-ops-{scenario}", "scenario_version": "1.0"})
            assert result.status_code == 201, result.text
            base = "/api/v1/simulation-runs/" + result.json()["id"]
            slots = client.get(base + "/incoming-handover").json()
            target = slots["tickets"][0]["ticket_url"]
            assert set(codes) <= catalog(target).keys()
            assert catalog(target)[codes[0]]["repeatable"]

            if scenario == "bmc":
                expected_results = [
                    ("CHECK_HOST_SERVICE", "NO_ISSUE", None, "응답 정상"),
                    ("OPEN_ALTERNATE_CONSOLE", "NO_ISSUE", None, "접속 성공"),
                    ("CHECK_MANAGEMENT_NETWORK", "INCONCLUSIVE", None, None),
                    ("RUN_BMC_DIAGNOSTICS", "ISSUE_FOUND", "BMC_TIMEOUT", "응답 시간 초과"),
                ]
                saved = []
                for action_code, status, error_code, observation in expected_results:
                    event = perform(target, action_code)
                    saved.append(event)
                    result = event["result"]
                    assert (result["diagnostic_status"], result["error_code"]) == (status, error_code)
                    if observation:
                        assert result["observations"][0]["value"] == observation
                    else:
                        assert result["unconfirmed"]
                assert client.get(target + "/actions").json()["activity"] == saved
                unconfirmed = perform(slots["tickets"][1]["ticket_url"], "RUN_BMC_DIAGNOSTICS")["result"]
                assert (unconfirmed["diagnostic_status"], unconfirmed["error_code"]) == ("INCONCLUSIVE", None)
                assert unconfirmed["observations"][0]["value"] is None
                host_id = client.get(target).json()["scenario_host_id"]
                hosts = client.get(base + "/hosts").json()["items"]
                assert next(host["state"]["error_code"] for host in hosts if host["id"] == host_id) == "BMC_TIMEOUT"

            if scenario == "major":
                action = target + "/actions"
                assert catalog(target)["RECORD_SHARED_TIMELINE"]["requires_note"]
                assert write(action, {"action_code": "RECORD_SHARED_TIMELINE"}).status_code == 422
                recorded = write(action, {"action_code": "RECORD_SHARED_TIMELINE", "note": "08:00 세 서비스 오류율 증가"})
                assert recorded.status_code == 201 and recorded.json()["note"] == "08:00 세 서비스 오류율 증가"

            if scenario == "network":
                perform(target, "SWITCH_REDUNDANT_PATH", 409)
                before = perform(target, "MEASURE_PACKET_LOSS")
                perform(target, "CHECK_REDUNDANT_PATH")
                assert catalog(target)["SWITCH_REDUNDANT_PATH"]["available"]
                switched = perform(target, "SWITCH_REDUNDANT_PATH")
                generic = perform(target, "RERUN_DIAGNOSTICS")
                assert switched["result"]["health"] == generic["result"]["health"] == "verification_pending"
                after = perform(target, "MEASURE_PACKET_LOSS")
                assert before["action_id"] != after["action_id"]
                assert before["result"]["health"] == "degraded"
                assert after["result"]["health"] == "healthy" and after["result"]["error_code"] is None
                perform(target, "SWITCH_REDUNDANT_PATH", 409)
                assert catalog(target)["MEASURE_PACKET_LOSS"]["available"]

                tickets = client.get(base + "/tickets").json()["items"]
                recurrence = next(ticket for ticket in tickets if ticket["title"] == "야간 링크 오류 재발")
                recurrence_path = base + "/tickets/" + recurrence["id"]
                history = client.get(recurrence_path + "/host-incidents").json()
                incident, = history["incidents"]
                assert incident["incident_key"] == "synthetic-ops-network-previous"
                assert client.get(target + "/host-incidents").json()["incidents"] == []
                evidence = {"kind": "HOST_HISTORY", "source_id": incident["incident_id"]}
                evidence_path = recurrence_path + "/learning-evidence"
                assert client.post(evidence_path, headers=auth, json={**evidence, "activity": "VIEWED"}).status_code == 201
                checked = perform(recurrence_path, "COMPARE_PORT_COUNTERS")
                eligible = client.get(evidence_path + "/actions", params=evidence).json()
                assert [item["action_id"] for item in eligible["items"]] == [checked["action_id"]]
                assert client.post(evidence_path, headers=auth, json={
                    **evidence, "activity": "USED", "action_event_id": checked["action_id"],
                }).status_code == 201

                missing = next(ticket for ticket in tickets if ticket["title"] == "단일 지점 통신 지연")
                missing_path = base + "/tickets/" + missing["id"]
                perform(missing_path, "CHECK_REDUNDANT_PATH")
                assert not catalog(missing_path)["SWITCH_REDUNDANT_PATH"]["available"]
                perform(missing_path, "SWITCH_REDUNDANT_PATH", 409)

            finalized = write(base + "/outgoing-handover/finalize", {"confirm": True, "expected_revision": 0})
            assert finalized.status_code == 200, finalized.text


def test_network_pilot_requires_observed_outcomes_and_authored_handover():
    def event(seq, code, outcome):
        return {"id": str(seq), "sequence": seq, "type": "virtual_action_performed",
                "details": {"action_code": code, "result": {"outcome": outcome}}}

    def verdict(key, *events, note=None, status="in_progress"):
        ticket = {"key": key, "asset_kind": "network", "status": status, "included": True, "work": [],
                  "resolution": None, "handover_next_action": "시드의 다음 조치", "handover_note": note, "events": events}
        return {rule["rule_code"]: rule["result"] for rule in grade(ticket, "report", flexible=True, network_pilot=True)["rules"]}

    switched = event(2, "SWITCH_REDUNDANT_PATH", "VERIFICATION_PENDING")
    pre_measure = event(1, "MEASURE_PACKET_LOSS", "MEASURED")
    post_measure = event(3, "MEASURE_PACKET_LOSS", "SERVICE_RECOVERED")
    assert verdict("ticket-01", pre_measure, switched)["POST_REPAIR_VALIDATION"] == "NOT_MET"
    assert verdict("ticket-01", switched, event(3, "MEASURE_PACKET_LOSS", "MEASURED"))["POST_REPAIR_VALIDATION"] == "NOT_MET"
    assert verdict("ticket-01", switched, post_measure)["DOCUMENT_ACCURACY"] == "NOT_MET"
    assert verdict("ticket-01", switched, post_measure, note="CRC 후속 점검", status="resolved")["HANDOVER_UNRESOLVED"] == "NOT_MET"

    missing = event(1, "COMPARE_PORT_COUNTERS", "MISSING_DATA")
    request = event(2, "REQUEST_PEER_MEASUREMENT", "REQUEST_RECORDED")
    assert verdict("ticket-02", missing)["APPROPRIATE_VIRTUAL_ACTION"] == "NOT_MET"
    assert verdict("ticket-02", request, event(3, "COMPARE_PORT_COUNTERS", "MISSING_DATA"))["APPROPRIATE_VIRTUAL_ACTION"] == "NOT_MET"
    assert verdict("ticket-02", missing, request)["HANDOVER_UNRESOLVED"] == "NOT_MET"
    held = verdict("ticket-02", missing, request, note="상대편 담당 · 21:00 KST 재확인")
    assert held["APPROPRIATE_VIRTUAL_ACTION"] == held["DOCUMENT_ACCURACY"] == held["HANDOVER_UNRESOLVED"] == "PASS"
    assert held["POST_REPAIR_VALIDATION"] == "NOT_APPLICABLE"


@pytest.mark.skipif(engine().url.database != "polytech_training_corpus" or os.getenv("APP_ENV") != "simulation",
                    reason="Requires the isolated seeded corpus and evaluation worker")
def test_network_pilot_evidence_from_search_to_evaluation():
    origin = {"Origin": settings().origin}
    account = {"username": f"network-qa-{uuid4().hex}", "password": uuid4().hex}

    with TestClient(app, base_url=settings().origin) as client:
        assert client.post("/api/v1/auth/register", json=account, headers=origin).status_code == 201
        login = client.post("/api/v1/auth/login", json=account, headers=origin)
        assert login.status_code == 200
        auth = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}

        def post(path, body, status=201, key=None):
            response = client.post(path, headers={**auth, "Idempotency-Key": key or uuid4().hex}, json=body)
            assert response.status_code == status, response.text
            return response.json()

        run = post("/api/v1/simulation-runs", {"scenario_key": NETWORK_SCENARIO, "scenario_version": NETWORK_VERSION})
        base = f'/api/v1/simulation-runs/{run["id"]}'
        first, second = client.get(base + "/incoming-handover").json()["tickets"]
        recovery, hold = first["ticket_url"], second["ticket_url"]
        sop, = client.get(hold).json()["related_sops"]
        assert f"## {SOP_SECTION}" in client.get(sop["source_url"]).json()["extracted_text"]
        hits = post(base + "/knowledge/search", {"ticket_id": second["ticket_id"], "source_types": ["SOP"],
                                                  "query": SOP_QUERY}, status=200)["results"]
        assert any(hit["source_id"] == sop["id"] and hit["section"] == SOP_SECTION for hit in hits)

        def action(ticket, code, status=201, key=None):
            return post(ticket + "/actions", {"action_code": code}, status=status, key=key)

        action(recovery, "CHECK_REDUNDANT_PATH")
        action(recovery, "SWITCH_REDUNDANT_PATH", 409)
        compared = action(recovery, "COMPARE_PORT_COUNTERS")["result"]
        assert [entry["value"] for entry in compared["observations"]] == [7, 130, 0]
        assert (compared["diagnostic_status"], compared["error_code"]) == ("ISSUE_FOUND", "LINK_FLAP")
        measured = action(recovery, "MEASURE_PACKET_LOSS")["result"]
        assert (measured["diagnostic_status"], measured["error_code"]) == ("ISSUE_FOUND", "PACKET_LOSS")
        switched = action(recovery, "SWITCH_REDUNDANT_PATH")
        assert switched["result"]["health"] == "verification_pending"
        assert action(recovery, "RERUN_DIAGNOSTICS")["result"]["health"] == "verification_pending"
        after = action(recovery, "MEASURE_PACKET_LOSS", key="recovery-measurement")
        assert action(recovery, "MEASURE_PACKET_LOSS", 200, key="recovery-measurement") == after
        assert after["result"]["outcome"] == "SERVICE_RECOVERED"
        loss = after["result"]["observations"][0]
        assert (loss["before"], loss["value"], loss["unit"], loss["window"]) == (4.0, 0.2, "%", "10분")
        assert (after["result"]["diagnostic_status"], after["result"]["error_code"]) == ("NO_ISSUE", None)
        assert after["result"]["unconfirmed"]
        toolbox = Toolbox(UUID(login.json()["user"]["id"]), UUID(first["ticket_id"]), run_id=UUID(run["id"]))
        history = asyncio.run(toolbox.execute("get_ticket_history", {"ticket_id": first["ticket_id"]}))
        assert history.status == "ok" and any("4.0% → 0.2%" in entry["content"] for entry in history.payload["untrusted_data"])

        action(hold, "REQUEST_PEER_MEASUREMENT", 409)
        missing = action(hold, "COMPARE_PORT_COUNTERS")
        assert missing["result"]["observations"][-1]["value"] is None
        assert (missing["result"]["diagnostic_status"], missing["result"]["error_code"]) == ("INCONCLUSIVE", None)
        assert action(hold, "CHECK_REDUNDANT_PATH")["result"]["outcome"] == "MISSING_DATA"
        assert action(hold, "SWITCH_REDUNDANT_PATH", 409)["error"]["code"] == "ACTION_PRECONDITION_FAILED"
        request = action(hold, "REQUEST_PEER_MEASUREMENT")
        assert request["result"]["outcome"] == "REQUEST_RECORDED"
        assert client.get(hold + "/actions").json()["activity"][-1] == request

        content = {"summary": "서비스 A 우회 복구, 서비스 B 확인 대기", "open_items": [
            {"ticket_id": first["ticket_id"], "next_action": "4% → 0.2%. 네트워크팀이 21:00 KST 기존 링크 CRC와 이중화를 확인."},
            {"ticket_id": second["ticket_id"], "next_action": "로컬 4%. 전환 보류. 상대편 담당이 21:00 KST 양단 표본과 우회 시험을 확인."},
        ]}
        draft = client.put(base + "/outgoing-handover", headers=auth, json={"expected_revision": 0, "content": content})
        assert draft.status_code == 200, draft.text
        post(base + "/outgoing-handover/finalize", {"confirm": True, "expected_revision": draft.json()["revision"]}, 200)
        deadline = time.monotonic() + 20
        while (summary := client.get(base + "/evaluation").json())["rules_status"] in {"PENDING", "PROCESSING"}:
            assert time.monotonic() < deadline, summary
            time.sleep(0.25)
        assert summary["rules_status"] == "READY", summary
        for ticket, evidence, validation in ((first, after, "PASS"), (second, request, "NOT_APPLICABLE")):
            result = client.get(f'{base}/evaluation/tickets/{ticket["ticket_id"]}').json()
            rules = {rule["rule_code"]: rule for rule in result["rule_results"]}
            assert rules["POST_REPAIR_VALIDATION"]["result"] == validation
            for code in ("APPROPRIATE_VIRTUAL_ACTION", "DOCUMENT_ACCURACY", "HANDOVER_UNRESOLVED"):
                assert rules[code]["result"] == "PASS"
            assert evidence["action_id"] in rules["DOCUMENT_ACCURACY"]["evidence_refs"]


@pytest.mark.skipif(engine().url.database != "polytech_training_corpus" or os.getenv("APP_ENV") != "simulation",
                    reason="Requires the isolated seeded corpus and evaluation worker")
def test_bmc_v11_distinguishes_management_fault_from_missing_evidence():
    origin = {"Origin": settings().origin}
    account = {"username": f"bmc-qa-{uuid4().hex}", "password": uuid4().hex}
    with TestClient(app, base_url=settings().origin) as client:
        assert client.post("/api/v1/auth/register", json=account, headers=origin).status_code == 201
        login = client.post("/api/v1/auth/login", json=account, headers=origin)
        assert login.status_code == 200
        auth = {**origin, "X-CSRF-Token": login.json()["csrf_token"]}

        def post(path, body, status=201, key=None):
            response = client.post(path, headers={**auth, "Idempotency-Key": key or uuid4().hex}, json=body)
            assert response.status_code == status, response.text
            return response.json()

        def action(path, code, status=201, *, note=None, key=None):
            return post(path + "/actions", {"action_code": code, "note": note}, status=status, key=key)

        run = post("/api/v1/simulation-runs", {"scenario_key": BMC_SCENARIO, "scenario_version": BMC_VERSION})
        base = f'/api/v1/simulation-runs/{run["id"]}'
        first, second = client.get(base + "/incoming-handover").json()["tickets"]
        investigation, hold = first["ticket_url"], second["ticket_url"]
        details = [client.get(path).json() for path in (investigation, hold)]
        assert [detail["title"] for detail in details] == [case[3] for case in corpus_bmc.BMC_ROW[-1][:2]]
        first_sop, = details[0]["related_sops"]
        assert details[1]["related_sops"] == [first_sop]
        document = client.get(first_sop["source_url"]).json()
        assert document["title"] == f"교육용 SOP · {corpus_bmc.SOP_TITLE}"
        assert f"## {corpus_bmc.SOP_SECTION}" in document["extracted_text"]
        assert "합성" not in document["extracted_text"]
        assert all("bmc_readings" not in host["state"] for host in client.get(base + "/hosts").json()["items"])
        hits = post(base + "/knowledge/search", {"ticket_id": second["ticket_id"], "source_types": ["SOP"],
                                                  "query": corpus_bmc.SOP_QUERY}, status=200)["results"]
        assert any(hit["source_id"] == first_sop["id"] and hit["section"] == corpus_bmc.SOP_SECTION for hit in hits)

        note = "업무 응답 30/30. 관리 접속 3회 실패. 서버 관리 담당이 20:30 KST에 재확인."
        action(investigation, "RECORD_REMAINING_RISK", 409, note=note)
        service = action(investigation, "CHECK_HOST_SERVICE")["result"]
        assert (service["diagnostic_status"], service["error_code"]) == ("NO_ISSUE", None)
        assert [row["value"] for row in service["observations"]] == [30, 30]
        diag = action(investigation, "RUN_BMC_DIAGNOSTICS")["result"]
        assert (diag["diagnostic_status"], diag["error_code"]) == ("ISSUE_FOUND", "BMC_TIMEOUT")
        assert [row["value"] for row in diag["observations"]] == [3, 3]
        management = action(investigation, "CHECK_MANAGEMENT_NETWORK")["result"]
        assert (management["diagnostic_status"], management["error_code"]) == ("NO_ISSUE", None)
        assert [row["value"] for row in management["observations"]] == [14, 12, 50]
        assert action(investigation, "OPEN_ALTERNATE_CONSOLE")["result"]["outcome"] == "CONSOLE_REACHABLE"
        followup = action(investigation, "RECORD_REMAINING_RISK", note=note)
        assert followup["result"]["outcome"] == "FOLLOWUP_RECORDED"

        action(hold, "REQUEST_ADDITIONAL_METRICS", 409)
        management = action(hold, "CHECK_MANAGEMENT_NETWORK")["result"]
        assert (management["diagnostic_status"], management["error_code"]) == ("INCONCLUSIVE", None)
        assert management["observations"][1]["value"] is None
        action(hold, "REQUEST_ADDITIONAL_METRICS", 409)
        assert action(hold, "OPEN_ALTERNATE_CONSOLE")["result"]["outcome"] == "MISSING_DATA"
        assert action(hold, "RUN_BMC_DIAGNOSTICS")["result"]["error_code"] is None
        requested = action(hold, "REQUEST_ADDITIONAL_METRICS", key="bmc-request")
        assert action(hold, "REQUEST_ADDITIONAL_METRICS", 200, key="bmc-request") == requested
        assert requested["result"]["outcome"] == "REQUEST_RECORDED"
        assert requested["result"]["observations"][-1]["value"] == "21:00 KST"
        assert client.get(hold + "/actions").json()["activity"][-1] == requested

        content = {"summary": "관리 접속 실패와 조사 보류 두 건을 인계합니다.", "open_items": [
            {"ticket_id": first["ticket_id"], "next_action": note},
            {"ticket_id": second["ticket_id"], "next_action": "관리망 담당이 비교 장비 표본과 콘솔 기록을 21:00 KST에 확인."},
        ]}
        draft = client.put(base + "/outgoing-handover", headers=auth, json={"expected_revision": 0, "content": content})
        assert draft.status_code == 200, draft.text
        post(base + "/outgoing-handover/finalize", {"confirm": True, "expected_revision": draft.json()["revision"]}, 200)
        deadline = time.monotonic() + 20
        while (summary := client.get(base + "/evaluation").json())["rules_status"] in {"PENDING", "PROCESSING"}:
            assert time.monotonic() < deadline, summary
            time.sleep(0.25)
        assert summary["rules_status"] == "READY", summary
        for ticket, evidence in ((first, followup), (second, requested)):
            result = client.get(f'{base}/evaluation/tickets/{ticket["ticket_id"]}').json()
            rules = {rule["rule_code"]: rule for rule in result["rule_results"]}
            for code in ("APPROPRIATE_VIRTUAL_ACTION", "DOCUMENT_ACCURACY", "HANDOVER_UNRESOLVED"):
                assert rules[code]["result"] == "PASS"
            assert evidence["action_id"] in rules["DOCUMENT_ACCURACY"]["evidence_refs"]
            assert rules["POST_REPAIR_VALIDATION"]["result"] == "NOT_APPLICABLE"
