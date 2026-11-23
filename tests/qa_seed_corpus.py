"""Load and verify the synthetic operations corpus in its dedicated Compose database."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from copy import deepcopy
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.agent.tools import Toolbox
from app.db import engine
from app.knowledge.documents import original_path, register_sop
from app.knowledge.embedding import embedding_profile
from app.knowledge.models import IndexJob, IndexStatus, KnowledgeChunk, SopDocument
from app.models import User
from app.settings import settings
from app.simulation.models import Scenario, ScenarioHost, ScenarioShiftSlot, ScenarioTicketTemplate
from app.tickets import Severity, TicketStatus
from tests.corpus_data import SCENARIOS, SOPS
from tests.corpus_expectations import EXPECTATIONS

PROJECT = "polytech-qa-synthetic-corpus"
DATABASE = "polytech_training_corpus"
PREFIX = "synthetic-ops-"
VERSION = "1.0"
KST = ZoneInfo("Asia/Seoul")
RESOLVED = {"resolved", "closed"}
HOURS = timedelta(hours=12)
ASSET = {
    "post": ("boot", "compute", "POST_STALL"),
    "boot-device": ("boot", "compute", "BOOT_DEVICE_MISSING"),
    "ecc": ("memory", "compute", "MEM_ECC_ERROR"),
    "memory-capacity": ("memory-pressure", "compute", "MEMORY_PRESSURE"),
    "storage-latency": ("storage", "storage", "STORAGE_LATENCY"),
    "storage-path": ("storage-path", "storage", "PATH_DOWN"),
    "link-flap": ("network", "network", "LINK_FLAP"),
    "network-path": ("network", "network", "PACKET_LOSS"),
    "power": ("power", "facilities", "PSU_REDUNDANCY_LOST"),
    "bmc": ("bmc", "management", "BMC_TIMEOUT"),
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def identifier(key):
    return uuid5(NAMESPACE_URL, f"polytech/{PROJECT}/{VERSION}/{key}")


def guarded_engine():
    origin = urlsplit(settings().origin)
    url = engine().url
    require(
        os.environ.get("CORPUS_PROJECT") == PROJECT and os.environ.get("APP_ENV") == "simulation"
        and os.environ.get("RUN_POSTGRES_TESTS") == "1" and os.environ.get("AGENT_PROVIDER") == "fake"
        and not os.environ.get("GEMINI_API_KEY")
        and origin.scheme == "http" and origin.hostname == "127.0.0.1" and origin.port not in (None, 80, 8080)
        and url.database == DATABASE and url.host == "db" and url.port == 5432 and url.username == "app_runtime",
        "Corpus writes require the dedicated loopback Fake QA Compose project.",
    )
    owner = create_engine(url.set(username="postgres", password=os.environ["TEST_DB_OWNER_PASSWORD"]),
                          hide_parameters=True, connect_args={"connect_timeout": 3})
    with owner.connect() as connection:
        identity = connection.execute(text("SELECT current_database(), current_user")).one()
        require(identity == (DATABASE, "postgres"), "Corpus database identity mismatch.")
    return owner


def sop_payload(key):
    title, scope, observe, act, verify = SOPS[key]
    return (
        f"# {title}\n\n가상 데이터센터 교육용 SOP. 모든 식별자와 수치는 합성입니다.\n"
        f"\n## 적용 조건\n{scope}\n\n## 관측과 진단\n{observe}\n"
        f"\n## 조치 순서\n{act}\n\n## 검증과 인계\n{verify}\n"
    ).encode()


def validate_data():
    require(len(SOPS) == 12 and len(SCENARIOS) == 8 and sum(len(row[-1]) for row in SCENARIOS.values()) == 40,
            "Expected 12 SOPs, 8 scenarios and 40 tickets.")
    require(SCENARIOS.keys() == EXPECTATIONS.keys(), "Assessment scenario keys differ.")
    used_sops = set()
    dispositions = set()
    titles = set()
    for key, (_, _, _, _, tickets) in SCENARIOS.items():
        require(len(tickets) == len(EXPECTATIONS[key]) == 5, f"{key}: invalid ticket count.")
        for case, expectation in zip(tickets, EXPECTATIONS[key], strict=True):
            status, priority, sop_key, title, symptom, diagnostic, performed, next_action = case
            require(status in TicketStatus and priority in Severity and sop_key in SOPS and title not in titles
                    and all((title, symptom, diagnostic, performed, next_action, *expectation)),
                    f"{key}: invalid ticket {title}.")
            require((status in RESOLVED) == (expectation[-1] == "resolved"), f"{key}: inconsistent resolution.")
            used_sops.add(sop_key)
            titles.add(title)
            dispositions.add(expectation[-1])
    require(used_sops == set(SOPS) and dispositions == {"handover", "resolved", "insufficient", "recurrence"},
            "Missing SOP relationship or scenario outcome.")


def ensure_sops(db, actor_id):
    documents = {}
    for key, sop in SOPS.items():
        payload = sop_payload(key)
        digest = hashlib.sha256(payload).hexdigest()
        filename = f"{PREFIX}{key}.md"
        title = "합성 SOP · " + sop[0]
        matches = db.scalars(select(SopDocument).where(SopDocument.filename == filename)).all()
        if matches:
            require(len(matches) == 1, f"Duplicate fixture filename: {filename}")
            document = matches[0]
            require(document.content_sha256 == digest and document.title == title
                    and original_path(document.id).read_bytes() == payload, f"SOP collision: {filename}")
        else:
            require(db.scalar(select(SopDocument.id).where(SopDocument.content_sha256 == digest)) is None,
                    f"SOP content already belongs to another record: {filename}")
            document, _ = register_sop(db, actor_id=actor_id, filename=filename, content_type="text/markdown",
                                       payload=payload, title=title)
        if not document.training_approved:
            document.training_approved = True
            db.commit()
        documents[key] = document.id
    return documents


def action_catalog(db):
    baseline = db.scalar(select(Scenario).where(Scenario.scenario_key == "dco-basic", Scenario.version == "1.1"))
    require(baseline is not None, "Baseline dco-basic v1.1 is missing.")
    catalog = deepcopy(baseline.config["action_catalog"])
    for action in catalog:
        if action["code"] in {"CHECK_SERVER_STATUS", "RUN_BMC_DIAGNOSTICS", "RERUN_DIAGNOSTICS"}:
            message = {
                "CHECK_SERVER_STATUS": "가상 호스트 상태를 확인했습니다.",
                "RUN_BMC_DIAGNOSTICS": "가상 진단을 실행했습니다. 관측 코드와 이전 교대 기록을 비교하세요.",
                "RERUN_DIAGNOSTICS": "현재 가상 상태를 재확인했습니다.",
            }[action["code"]]
            for asset in {"boot", "memory-pressure", "storage-path", "network", "power", "bmc", "incident"}:
                action["variants"][asset] = {"message": message}
    return catalog


def build_scenarios(db, documents):
    catalog = action_catalog(db)
    for position, (key, row) in enumerate(SCENARIOS.items()):
        title, family, default_service, default_code, cases = row
        scenario_key = PREFIX + key
        scenario_id = identifier(scenario_key)
        start = datetime(2026, 11, 10 + position, 8 if position % 2 == 0 else 20, tzinfo=KST)
        crews = ["ABCD"[(position + slot) % 4] for slot in range(3)]
        config = {
            "incoming_report": {
                "summary": f"{crews[0]}조 Bot이 {cases[0][3]}과 {cases[1][3]}을 인계합니다. "
                           f"완료된 {cases[2][3]}은 참고 기록입니다. 현재 근무에서 남은 위험을 확인해 {crews[2]}조 Bot으로 넘기세요.",
                "ticket_templates": ["ticket-01", "ticket-02"],
            },
            "evaluation_rule_version": "synthetic-corpus-1",
            "action_catalog": catalog,
        }
        hosts = []
        templates = []
        for index, (status, severity, sop, subject, symptom, diagnostic, performed, next_action) in enumerate(cases):
            ticket_key = f"ticket-{index + 1:02}"
            host_id = identifier(f"{key}/{ticket_key}/host")
            asset, service, code = ASSET.get(sop, (family, default_service, default_code))
            resolved = status in RESOLVED
            code_missing = index == 4 or asset == "memory-pressure"
            host_state = {"asset_kind": asset, "health": "healthy" if resolved else "degraded",
                          "error_code": None if resolved or code_missing else code}
            if asset == "memory" and not resolved:
                host_state["replacement_done"] = False
            hosts.append({"id": host_id, "scenario_id": scenario_id,
                          "host_key": f"SIM-{key.upper()}-{index + 1:02}",
                          "hostname": f"synthetic-{key}-{index + 1:02}.example.invalid",
                          "aliases": [], "initial_state": host_state})
            previous = index < 3
            detected = start - timedelta(minutes=180 - index * 30) if previous else start
            observed = detected + timedelta(minutes=4 if previous else 0)
            finished = detected + timedelta(minutes=8) if resolved else None
            crew = crews[0] + "조 Bot" if previous else "교대 시작 관측"
            history = [{"at": observed.isoformat(), "crew": crew, "observation": f"진단 로그: {diagnostic}"},
                       {"at": (finished or observed).isoformat(), "crew": crew, "observation": f"조치 기록: {performed}"}]
            fields = {
                "title": subject, "description": f"[교육용] {symptom}\n관측 로그: {diagnostic}",
                "service": service, "severity": severity, "status": status,
                "impact": "서비스 기준선 복귀를 확인함" if resolved else
                          ("영향 범위 확인 중" if severity == "P4" else f"합성 {service} 서비스 관측 범위에서 영향 확인"),
                "resolution": performed if resolved else None, "resolved_at": finished.isoformat() if finished else None,
                "next_action": next_action,
                "recheck_at": (start + timedelta(hours=1)).isoformat() if status == "waiting" else None,
                "sop_refs": [str(documents[sop])], "observed_history": history,
            }
            require(all(detected <= datetime.fromisoformat(entry["at"]) <= start for entry in history),
                    f"{key}/{ticket_key}: observation outside initial shift boundary.")
            templates.append({"id": identifier(f"{key}/{ticket_key}"), "scenario_id": scenario_id,
                              "scenario_host_id": host_id, "template_key": ticket_key, "initial_ticket": fields,
                              "detected_at": detected, "origin": "CARRYOVER" if previous else "INITIAL"})
        # Preserve the manifest of previously published v1.0 templates through the display-label change.
        manifest_templates = deepcopy(templates)
        for template in manifest_templates:
            fields = template["initial_ticket"]
            fields["description"] = "[합성]" + fields["description"].removeprefix("[교육용]")
        manifest = hashlib.sha256(json.dumps([config, hosts, manifest_templates], sort_keys=True,
                                            ensure_ascii=False, default=str).encode())
        config["corpus_manifest_sha256"] = manifest.hexdigest()
        existing = db.scalar(select(Scenario).where(Scenario.scenario_key == scenario_key, Scenario.version == VERSION))
        if existing:
            require(existing.id == scenario_id and existing.is_published and existing.config == config,
                    f"Published scenario collision: {scenario_key}")
            continue
        scenario = Scenario(id=scenario_id, scenario_key=scenario_key, version=VERSION, title="[교육용] " + title,
                            timezone=KST.key, virtual_start_at=start, is_published=False, config=config)
        db.add(scenario)
        db.flush()
        for slot in range(3):
            begins = start + (slot - 1) * HOURS
            db.add(ScenarioShiftSlot(id=identifier(f"{key}/shift/{slot}"), scenario_id=scenario_id, slot_order=slot,
                                     crew_code=crews[slot], shift_type="DAY" if begins.hour == 8 else "NIGHT",
                                     actor_type="TRAINEE" if slot == 1 else "BOT", starts_at=begins, ends_at=begins + HOURS))
        db.add_all(ScenarioHost(**host) for host in hosts)
        db.flush()
        db.add_all(ScenarioTicketTemplate(**template) for template in templates)
        db.flush()
        scenario.is_published = True
        db.flush()


def seed(owner, username):
    with owner.connect() as guard:
        guard.execute(text("SELECT pg_advisory_lock(hashtext(:project))"), {"project": PROJECT})
        try:
            with Session(owner, expire_on_commit=False) as db:
                actor_id = db.scalar(select(User.id).where(User.username == username, User.role == "operator",
                                                           User.is_active.is_(True)))
                require(actor_id is not None, "Active QA operator not found.")
                documents = ensure_sops(db, actor_id)
                build_scenarios(db, documents)
                db.commit()
                return documents
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(hashtext(:project))"), {"project": PROJECT})


def wait_for_index(documents):
    deadline = time.monotonic() + 90
    profile = embedding_profile().fingerprint
    ids = list(documents.values())
    while time.monotonic() < deadline:
        with Session(engine()) as db:
            jobs = db.scalars(select(IndexJob).where(IndexJob.id.in_(ids))).all()
            failed = [str(job.id) for job in jobs if job.status == IndexStatus.FAILED]
            require(not failed, f"Failed SOP index jobs: {failed}")
            if len(jobs) == len(ids) and all(job.status == IndexStatus.COMPLETED and job.indexed_profile == profile for job in jobs):
                chunks = db.execute(select(KnowledgeChunk.source_id, func.count()).where(
                    KnowledgeChunk.source_id.in_(ids), KnowledgeChunk.embedding_profile == profile,
                ).group_by(KnowledgeChunk.source_id)).all()
                require(len(chunks) == len(ids), "Missing search chunks.")
                return sum(count for _, count in chunks)
        time.sleep(1)
    raise RuntimeError("SOP indexing timed out. Start the dedicated Fake worker.")


def verify(owner, documents):
    from main import app

    chunks = wait_for_index(documents)
    with Session(owner) as db:
        ids = db.scalars(select(Scenario.id).where(Scenario.scenario_key.startswith(PREFIX), Scenario.is_published)).all()
        tickets = db.scalar(select(func.count()).select_from(ScenarioTicketTemplate).where(ScenarioTicketTemplate.scenario_id.in_(ids)))
        linked_documents = db.scalar(select(func.count()).select_from(SopDocument).where(
            SopDocument.filename.startswith(PREFIX), SopDocument.training_approved,
        ))
        require((linked_documents, tickets, len(ids)) == (12, 40, 8), "Persisted fixture counts differ.")

    origin = settings().origin
    auth = {"Origin": origin}
    username, password = "synthetic-corpus-verifier", "qa-corpus-only-password"
    credentials = {"username": username, "password": password}

    with TestClient(app, base_url=origin) as client:
        login = client.post("/api/v1/auth/login", json=credentials, headers=auth)
        if login.status_code == 401:
            require(client.post("/api/v1/auth/register", json=credentials, headers=auth).status_code == 201,
                    "Could not register the isolated QA verifier.")
            login = client.post("/api/v1/auth/login", json=credentials, headers=auth)
        require(login.status_code == 200 and login.json()["roles"] == ["trainee"], "QA verifier login failed.")
        auth["X-CSRF-Token"] = login.json()["csrf_token"]
        catalog = client.get("/api/v1/simulation").json()["available_scenarios"]
        require(all(item["scenario_key"] != "dco-basic" for item in catalog),
                "Legacy scenarios appeared in the corpus start list.")
        require(all(item["title"].startswith("[교육용]") for item in catalog if item["scenario_key"].startswith(PREFIX)),
                "Incorrect education title prefix.")
        searched = set()
        observed = 0
        agent_context_checks = 0
        rubric_text = {value for rows in EXPECTATIONS.values() for row in rows for value in row[:3] if len(value) >= 24}
        for key, row in SCENARIOS.items():
            start = client.post("/api/v1/simulation-runs", json={"scenario_key": PREFIX + key, "scenario_version": VERSION},
                                headers={**auth, "Idempotency-Key": f"corpus-v1-start-{key}"})
            require(start.status_code in (200, 201), f"{key}: start failed ({start.status_code}).")
            base = "/api/v1/simulation-runs/" + start.json()["id"]
            slots = client.get(base + "/schedule").json()["slots"]
            require([slot["actor_type"] for slot in slots] == ["BOT", "TRAINEE", "BOT"]
                    and slots[0]["ends_at"] == slots[1]["starts_at"] and slots[1]["ends_at"] == slots[2]["starts_at"]
                    and all(datetime.fromisoformat(slot["ends_at"]) - datetime.fromisoformat(slot["starts_at"]) == HOURS
                            for slot in slots), f"{key}: incorrect shift sequence.")
            all_tickets = client.get(base + "/tickets?scope=all").json()["items"]
            require(len(all_tickets) == 5, f"{key}: expected 5 run-scoped tickets.")
            by_title = {ticket["title"]: ticket for ticket in all_tickets}
            incoming = client.get(base + "/incoming-handover").json()["tickets"]
            require([link["ticket_id"] for link in incoming] == [by_title[case[3]]["id"] for case in row[-1][:2]],
                    f"{key}: incoming report is not ordered.")
            for case in row[-1]:
                status, severity, sop_key, title, _, diagnostic, performed, next_action = case
                ticket = by_title[title]
                path = base + "/tickets/" + ticket["id"]
                detail = client.get(path).json()
                refs = detail["related_sops"]
                require(detail["description"].startswith("[교육용]"), f"{key}: incorrect ticket description prefix.")
                detected = datetime.fromisoformat(ticket["detected_at"])
                shift_start = datetime.fromisoformat(slots[1]["starts_at"])
                previous_start = datetime.fromisoformat(slots[0]["starts_at"])
                require((previous_start <= detected < shift_start if ticket["training_origin"] == "CARRYOVER"
                         else detected == shift_start), f"{key}: detection time does not match its shift.")
                require(ticket["status"] == status and ticket["severity"] == severity and ticket["next_action"] == next_action
                        and [ref["id"] for ref in refs] == [str(documents[sop_key])], f"{key}: ticket or SOP mismatch.")
                source = client.get(refs[0]["source_url"])
                require(source.status_code == 200, f"{key}: approved SOP link failed.")
                history = client.get(path + "/operations").json()["history"]
                seed_history = [event for event in history if event["type"] == "scenario_observation"]
                require(len(seed_history) == 2 and diagnostic in seed_history[-1]["details"]["reason"]
                        and performed in seed_history[0]["details"]["reason"]
                        and datetime.fromisoformat(seed_history[-1]["created_at"]) <= datetime.fromisoformat(seed_history[0]["created_at"]),
                        f"{key}: missing or out-of-order seed history.")
                observed += len(seed_history)
                if sop_key not in searched:
                    search = client.post(base + "/knowledge/search", headers=auth, json={
                        "query": SOPS[sop_key][0], "source_types": ["SOP"], "ticket_id": ticket["id"], "limit": 5,
                    })
                    require(search.status_code == 200 and str(documents[sop_key]) in
                            {hit["source_id"] for hit in search.json()["results"]}, f"SOP recall@5 failed: {sop_key}")
                    toolbox = Toolbox(UUID(login.json()["user"]["id"]), UUID(ticket["id"]), run_id=UUID(start.json()["id"]))
                    for name, params in (
                        ("get_ticket_details", {"ticket_id": ticket["id"]}),
                        ("get_ticket_history", {"ticket_id": ticket["id"]}),
                        ("search_sop", {"query": SOPS[sop_key][0], "limit": 5}),
                    ):
                        result = asyncio.run(toolbox.execute(name, params))
                        require(result.status == "ok" and result.payload.get("untrusted_data"), f"AI tool failed: {name}.")
                        serialized = json.dumps(result.payload, ensure_ascii=False)
                        require(not any(value in serialized for value in rubric_text), f"Assessment rubric in {name}.")
                        if name == "search_sop":
                            require(str(documents[sop_key]) in {item["source_id"] for item in result.payload["untrusted_data"]},
                                    f"AI SOP recall@5 failed: {sop_key}.")
                        agent_context_checks += 1
                    searched.add(sop_key)
            if start.json()["status"] == "ACTIVE":
                result = client.post(base + "/outgoing-handover/finalize", json={"confirm": True, "expected_revision": 0},
                                     headers={**auth, "Idempotency-Key": f"corpus-v1-finish-{key}"})
                require(result.status_code == 200, f"{key}: handover finalization failed ({result.status_code}).")
            report = client.get(base + "/outgoing-handover").json()
            require(report["status"] == "FINALIZED" and len(report["items"]) == 4
                    and all(item["status"] not in RESOLVED for item in report["items"]), f"{key}: outgoing selection mismatch.")
        require(len(searched) == len(SOPS), "SOP query coverage incomplete.")
        client.post("/api/v1/auth/logout", headers=auth)
    return {"sops": 12, "ticket_templates": 40, "scenarios": 8, "sop_chunks": chunks, "sop_recall_at_5": len(searched),
            "verified_runs": 8, "observed_history_events": observed, "offline_expectations": 40,
            "agent_context_checks": agent_context_checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the corpus into the dedicated test database")
    parser.add_argument("--verify", action="store_true", help="Verify indexing, search and scenario start/finalization")
    parser.add_argument("--username", default="demo", help="Existing QA operator used to register SOP originals")
    args = parser.parse_args()
    validate_data()
    if not args.apply:
        if args.verify:
            parser.error("--verify requires --apply and creates isolated QA verification runs")
        print(json.dumps({"sops": 12, "ticket_templates": 40, "scenarios": 8, "offline_expectations": 40}))
        return
    owner = guarded_engine()
    try:
        documents = seed(owner, args.username)
        result = verify(owner, documents) if args.verify else {"sops": len(documents), "ticket_templates": 40, "scenarios": 8}
        print(json.dumps(result))
    finally:
        owner.dispose()


if __name__ == "__main__":
    main()
