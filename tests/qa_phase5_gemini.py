"""Run on a disposable Gemini QA worker. The only inputs and profiles come from synthetic fixtures."""
import argparse
import asyncio
import json
import os
import re
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent.provider import AnalysisFailure, generator
from app.db import engine
from app.knowledge.embedding import EmbeddingFailure, get_embedder
from app.simulation.evaluation_feedback import explain
from app.simulation.evaluation_rules import grade
from app.simulation.learning_materials import TITLE, seed
from app.simulation.models import ScenarioIncident

MEMORY = UUID("d3000000-0000-4000-8000-000000000012")
STORAGE = UUID("d3000000-0000-4000-8000-000000000011")


def seed_corpus():
    provider = get_embedder()
    assert provider.profile.provider == "gemini"
    with Session(engine()) as db:
        document_id = seed(db, "demo").id
        fixtures = db.scalars(select(ScenarioIncident).where(ScenarioIncident.id.in_([MEMORY, STORAGE]))).all()
        assert {row.id for row in fixtures} == {MEMORY, STORAGE} and all(row.approved for row in fixtures)
    vectors = {row.id: provider.documents(row.title, [row.resolution])[0] for row in fixtures}
    owner = create_engine(engine().url.set(username="postgres", password=os.environ["QA_GEMINI_DB_OWNER"]), hide_parameters=True)
    try:
        with Session(owner) as db, db.begin():
            for incident_id, vector in vectors.items():
                row = db.get(ScenarioIncident, incident_id, with_for_update=True)
                row.embedding, row.embedding_profile = vector, provider.profile.fingerprint
    finally:
        owner.dispose()
    return str(document_id)


def get(client, path):
    reply = client.get(path)
    reply.raise_for_status()
    return reply.json()


def wait_until(callback, timeout=130):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := callback():
            return result
        time.sleep(.7)
    raise TimeoutError("A synthetic Gemini job did not finish before the deadline.")


def retrieval(origin, sop_id):
    username = "p5-gemini-" + uuid4().hex
    body = {"username": username, "password": "synthetic-gemini-phase5-password"}
    with httpx2.Client(base_url="http://api:8000/api/v1", timeout=25, trust_env=False, headers={"Origin": origin}) as client:
        client.post("/auth/register", json=body).raise_for_status()
        login = client.post("/auth/login", json=body)
        login.raise_for_status()
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        started = client.post("/simulation-runs", headers={"Idempotency-Key": str(uuid4())},
                              json={"scenario_key": "dco-basic", "scenario_version": "1.1"})
        started.raise_for_status()
        run = "/simulation-runs/" + started.json()["id"]
        source = run + "/sops/" + sop_id
        status = wait_until(lambda: (state if (state := get(client, source)["status"]) in {"COMPLETED", "FAILED"} else None))
        assert status == "COMPLETED"
        target = next(ticket for ticket in get(client, run + "/tickets")["items"] if ticket["training_origin"] == "INITIAL")
        target_path = run + "/tickets/" + target["id"]
        probes = {
            "related_memory": "합성 메모리 ECC 오류 가상 메모리 점검과 교체 뒤 재진단 MEM_ECC_ERROR",
            "unrelated_korean": "제주도 감귤 케이크 조리법과 재료는 무엇인가요?",
            "unrelated_english": "How do migratory birds navigate above the ocean at night?",
        }
        found = {}
        for name, query in probes.items():
            response = client.post(run + "/knowledge/search", json={"source_types": ["SOP", "INCIDENT"],
                                   "query": query, "ticket_id": target["id"], "limit": 5})
            response.raise_for_status()
            items = response.json()["results"]
            for item in items:
                assert item["source_id"] in {sop_id, str(MEMORY), str(STORAGE)}
                client.get(item["source_url"].removeprefix("/api/v1")).raise_for_status()
            found[name] = [{"kind": item["source_type"], "score": item["score"]} for item in items]
        client.get(source + "/original").raise_for_status()
        prompts = {
            "related": "현재 합성 메모리 진단 경보의 티켓을 읽고, MEM_ECC_ERROR 관련 SOP와 같은 시나리오의 과거 장애를 각각 검색해 관련 출처를 인용하세요. 과거 장애와 현재 티켓을 구분하세요.",
            "unrelated": "장애와 무관한 질문입니다. 제주도 감귤 케이크 재료를 알고 싶습니다. 티켓 원인을 억측하지 마세요.",
        }
        analysis = {}
        for name, prompt in prompts.items():
            submitted = client.post(target_path + "/analyses", json={"message": prompt})
            submitted.raise_for_status()
            status_url = submitted.headers["location"].removeprefix("/api/v1")

            def terminal():
                row = get(client, status_url)
                return row if row["status"] in {"failed", "succeeded"} else None

            row = wait_until(terminal)
            result = row["result"] or {}
            entries = result.get("evidence", [])
            cited = {item["key"]: item for item in entries}
            for item in entries:
                client.get(item["source_url"].removeprefix("/api/v1")).raise_for_status()
            direct = {"TICKET_DETAILS", "TICKET_HISTORY", "OPEN_TICKET"}
            for statement in result.get("confirmed_facts", []):
                assert statement["citation_keys"] and all(cited[key]["kind"] in direct for key in statement["citation_keys"])
            analysis[name] = {"status": row["status"], "provider": row["provider"], "error": (row["error"] or {}).get("code"),
                              "sources": sorted({item["kind"] for item in entries}),
                              "confirmed": [item["text"] for item in result.get("confirmed_facts", [])],
                              "hypotheses": [item["text"] for item in result.get("hypotheses", [])],
                              "limitations": result.get("limitations", [])}
    return {"search": found, "analysis": analysis}


async def feedback():
    model = generator()
    assert model.provider == "gemini"
    report_id = "00000000-0000-4000-8000-000000000099"
    prepared = {
        "no_action": [],
        "recheck_missing": ["RUN_BMC_DIAGNOSTICS", "REPLACE_MEMORY"],
        "verified_unresolved": ["RUN_BMC_DIAGNOSTICS", "REPLACE_MEMORY", "RERUN_DIAGNOSTICS"],
    }
    results = {}
    try:
        for case, actions in prepared.items():
            ticket = {"asset_kind": "memory", "status": "in_progress", "included": case == "verified_unresolved",
                      "work": [], "resolution": None, "warnings": [],
                      "handover_next_action": "synthetic" if case == "verified_unresolved" else None,
                      "events": [{"type": "virtual_action_performed", "id": str(uuid4()), "sequence": index,
                                  "details": {"action_code": action}} for index, action in enumerate(actions, 1)]}
            fixed = grade(ticket, report_id)
            variants = [await explain(model, ticket, fixed["rules"], fixed["evidence"]) for _ in range(3)]
            if not actions:
                assert all(not output["strengths"] for output in variants)
            assert all(output["recommendations"] for output in variants)
            versions = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in variants}
            results[case] = {"calls": len(variants), "unique_outputs": len(versions),
                             "status": "validated", "representative": variants[0],
                             "rules": {rule["rule_code"]: rule["result"] for rule in fixed["rules"]}}
        return {"model": model.model, "cases": results}
    finally:
        await model.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--mode", choices=("retrieval", "feedback"), required=True)
    args = parser.parse_args()
    url = urlsplit(args.origin)
    if (not re.fullmatch(r"polytech-qa-phase5-gemini(?:-[a-z0-9]+)*", args.project) or url.scheme != "http"
            or url.hostname != "127.0.0.1" or url.port in (None, 80, 8080) or url.path):
        parser.error("Use the disposable Gemini QA worker and its own loopback origin.")
    try:
        output = asyncio.run(feedback()) if args.mode == "feedback" else retrieval(args.origin, seed_corpus())
        print(json.dumps(output, ensure_ascii=True, indent=2))
    except (AnalysisFailure, EmbeddingFailure) as failure:
        print(json.dumps({"error": failure.code}))
        raise SystemExit(1) from None
    except httpx2.HTTPStatusError as failure:
        print(json.dumps({"http_status": failure.response.status_code, "path": failure.request.url.path}))
        raise SystemExit(1) from None
