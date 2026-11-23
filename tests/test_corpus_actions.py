import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import engine
from app.settings import settings
from main import app


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

            if scenario == "major":
                action = target + "/actions"
                assert catalog(target)["RECORD_SHARED_TIMELINE"]["requires_note"]
                assert write(action, {"action_code": "RECORD_SHARED_TIMELINE"}).status_code == 422
                recorded = write(action, {"action_code": "RECORD_SHARED_TIMELINE", "note": "08:00 세 서비스 오류율 증가"})
                assert recorded.status_code == 201 and recorded.json()["note"] == "08:00 세 서비스 오류율 증가"

            if scenario == "network":
                def perform(path, code, status=201):
                    response = write(path + "/actions", {"action_code": code})
                    assert response.status_code == status, response.text
                    return response.json()

                perform(target, "SWITCH_REDUNDANT_PATH", 409)
                before = perform(target, "MEASURE_PACKET_LOSS")
                perform(target, "CHECK_REDUNDANT_PATH")
                assert catalog(target)["SWITCH_REDUNDANT_PATH"]["available"]
                perform(target, "SWITCH_REDUNDANT_PATH")
                after = perform(target, "MEASURE_PACKET_LOSS")
                assert before["action_id"] != after["action_id"]
                assert before["result"]["health"] == "degraded"
                assert after["result"]["health"] == "healthy" and after["result"]["error_code"] is None
                perform(target, "SWITCH_REDUNDANT_PATH", 409)
                assert catalog(target)["MEASURE_PACKET_LOSS"]["available"]

                tickets = client.get(base + "/tickets").json()["items"]
                missing = next(ticket for ticket in tickets if ticket["title"] == "단일 지점 통신 지연")
                missing_path = base + "/tickets/" + missing["id"]
                perform(missing_path, "CHECK_REDUNDANT_PATH")
                assert not catalog(missing_path)["SWITCH_REDUNDANT_PATH"]["available"]
                perform(missing_path, "SWITCH_REDUNDANT_PATH", 409)

            finalized = write(base + "/outgoing-handover/finalize", {"confirm": True, "expected_revision": 0})
            assert finalized.status_code == 200, finalized.text
