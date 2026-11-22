"""Check restart, database outage, and restore into a fresh synthetic Compose project."""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import time
from uuid import uuid4

import httpx2

from tests.qa_roster import PASSWORD, USERNAMES

TABLES = (
    "alembic_version", "tickets", "ticket_events", "ticket_work", "handovers", "handover_items",
    "handover_revisions", "sop_documents", "index_jobs", "knowledge_chunks", "agent_runs", "agent_tool_calls",
    "simulation_scenarios", "scenario_shift_slots", "scenario_hosts", "scenario_ticket_templates",
    "simulation_runs", "simulation_run_hosts", "scenario_incidents",
)
EVALUATION_TABLES = ("simulation_evaluations", "simulation_ticket_evaluations", "simulation_learning_events")


def docker(*args, input_data=None):
    executable = os.environ.get("QA_DOCKER_EXECUTABLE", "docker")
    return subprocess.run([executable, *args], input=input_data, stdout=subprocess.PIPE, check=True).stdout


def compose(project, *args, development=False, input_data=None):
    files = ["-f", "compose.yaml"]
    if development:
        files.extend(["-f", os.environ.get("QA_COMPOSE_OVERRIDE", "compose.override.yaml")])
    scope = "SOURCE" if project == os.environ.get("QA_PERSISTENCE_SOURCE") else "RESTORE"
    if network := os.environ.get(f"QA_PERSISTENCE_{scope}_NETWORKS"):
        files.extend(["-f", network])
    return docker("compose", "--env-file", ".env.example", "-p", project, *files, *args, input_data=input_data)


def fingerprint(project, tables=TABLES):
    sql = " UNION ALL ".join(
        f"SELECT '{table}', count(*), md5(string_agg(serialized, '\n' ORDER BY serialized)) "
        f"FROM (SELECT to_jsonb(entry)::text AS serialized FROM {table} AS entry) AS encoded"
        for table in tables
    ) + " ORDER BY 1"
    output = compose(project, "exec", "-T", "db", "psql", "-U", "postgres", "-d", "polytech",
                     "-At", "-v", "ON_ERROR_STOP=1", "-c", sql)
    return {table: (int(count), digest) for table, count, digest in
            (line.split("|") for line in output.decode().splitlines())}


def sop_archive(project, *, development=False):
    return compose(project, "run", "--rm", "--no-deps", "-T", "worker",
                   "tar", "-C", "/var/lib/polytech/sops", "-cf", "-", ".", development=development)


def file_digests(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {item.name: hashlib.sha256(archive.extractfile(item).read()).hexdigest()
                for item in archive.getmembers() if item.isfile()}


def api_readiness(project):
    probe = """
import json
from urllib.error import HTTPError
from urllib.request import urlopen

try:
    response = urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=15)
except HTTPError as error:
    response = error
with response:
    print(json.dumps([response.status, json.load(response)]))
"""
    return json.loads(compose(project, "exec", "-T", "api", "python", "-c", probe, development=True))


def wait_for_stack(project, timeout):
    compose(project, "up", "--no-recreate", "--no-build", "--wait", "--wait-timeout", str(timeout),
            "nginx", "worker", development=True)
    if api_readiness(project) != [200, {"status": "ready"}]:
        raise RuntimeError("The internal API did not report database readiness.")


def ensure_simulation_fixture(origin):
    with httpx2.Client(base_url=origin, timeout=20, trust_env=False) as client:
        operator_login = client.post("/api/v1/auth/login", headers={"Origin": origin}, json={
            "username": USERNAMES[0], "password": PASSWORD,
        })
        operator_login.raise_for_status()
        state = client.get("/api/v1/simulation")
        state.raise_for_status()
        if not state.json()["active_run_id"]:
            response = client.post("/api/v1/simulation-runs", headers={
                "Origin": origin, "X-CSRF-Token": operator_login.json()["csrf_token"], "Idempotency-Key": "qa-persistence-start",
            }, json={"scenario_key": "dco-basic", "scenario_version": "1.0"})
            response.raise_for_status()


def login(client, origin, username, password):
    response = client.post("/auth/login", headers={"Origin": origin}, json={"username": username, "password": password})
    response.raise_for_status()
    client.headers.update({"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]})


def new_training_run(client):
    response = client.post("/simulation-runs", headers={"Idempotency-Key": str(uuid4())},
                           json={"scenario_key": "dco-basic", "scenario_version": "1.1"})
    response.raise_for_status()
    return "/simulation-runs/" + response.json()["id"]


def finish_training(client, run, revision=0):
    key = str(uuid4())
    body = {"confirm": True, "expected_revision": revision}
    response = client.post(run + "/outgoing-handover/finalize", headers={"Idempotency-Key": key}, json=body)
    response.raise_for_status()
    return key, body, response.json()


def json_get(client, path):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def wait_for_evaluation(client, run, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = json_get(client, run + "/evaluation")
        states = [summary["rules_status"], summary["explanation_status"]]
        if states == ["READY", "READY"]:
            return summary
        if "FAILED" in states:
            raise RuntimeError("The isolated Fake evaluation failed.")
        time.sleep(.4)
    raise TimeoutError("The restored evaluation worker did not publish before the deadline.")


def evidence_views(client, run, ticket):
    return {path: json_get(client, path) for path in (
        run, run + "/outgoing-handover", run + "/evaluation", run + "/evaluation/tickets",
        run + "/evaluation/tickets/" + ticket, run + "/tickets/" + ticket + "/actions",
        run + "/tickets/" + ticket + "/operations", run + "/events",
    )}


def evaluation_fixture(client, origin, timeout):
    username, password = "qa-persistence-" + uuid4().hex, "synthetic-persistence-account"
    response = client.post("/auth/register", headers={"Origin": origin}, json={"username": username, "password": password})
    response.raise_for_status()
    login(client, origin, username, password)
    run = new_training_run(client)
    ticket = next(row for row in json_get(client, run + "/tickets")["items"] if row["training_origin"] == "CARRYOVER")
    path = run + "/tickets/" + ticket["id"]
    incident = json_get(client, path + "/host-incidents")["incidents"][0]["id"]
    for resource, body, headers in (
        ("learning-evidence", {"kind": "HOST_HISTORY", "activity": "VIEWED", "source_id": incident}, {}),
        ("actions", {"action_code": "RUN_BMC_DIAGNOSTICS", "parameters": {"target": "storage"}},
         {"Idempotency-Key": str(uuid4())}),
    ):
        client.post(f"{path}/{resource}", json=body, headers=headers).raise_for_status()
    version = json_get(client, path)["version"]
    client.post(path + "/work", json={"expected_version": version, "title": "합성 재확인 작업", "status": "todo"}).raise_for_status()
    draft = client.put(run + "/outgoing-handover", json={"expected_revision": 0, "refresh_snapshot": True,
        "content": {"summary": "합성 백업 검증", "open_items": [{"ticket_id": ticket["id"], "next_action": "지연 다시 점검"}]}})
    draft.raise_for_status()
    receipt = finish_training(client, run, draft.json()["revision"])
    wait_for_evaluation(client, run, timeout)
    views = evidence_views(client, run, ticket["id"])
    detail = views[run + "/evaluation/tickets/" + ticket["id"]]
    if not detail["feedback"] or not detail["rule_results"] or not detail["evidence"]:
        raise RuntimeError("The synthetic backup must contain published rules, explanation and evidence.")
    return username, password, run, ticket["id"], receipt, views


def change_lease(project, run, *, expired=False):
    # UUID validation keeps the controlled QA SQL restricted to the fixture created by this run.
    from uuid import UUID

    run_id = UUID(run.rsplit("/", 1)[1])
    assignment = "rules_lease_until=now()-interval '1 second'" if expired else (
        f"rules_status='PROCESSING', rules_attempts=1, rules_lease_token='{uuid4()}', rules_lease_until=now()+interval '30 minutes'"
    )
    expected = "PROCESSING" if expired else "PENDING"
    result = compose(project, "exec", "-T", "db", "psql", "-U", "postgres", "-d", "polytech", "-Atq",
                     "-v", "ON_ERROR_STOP=1", "-c", f"UPDATE simulation_evaluations SET {assignment} "
                     f"WHERE simulation_run_id='{run_id}' AND rules_status='{expected}' RETURNING simulation_run_id")
    if result.decode().strip() != str(run_id):
        raise RuntimeError("The isolated evaluation lease could not be prepared.")


def verify(project, target, origin, wait_timeout, restore_origin=None):
    published = compose(project, "port", "nginx", "80", development=True).decode().strip()
    if f"http://{published}" != origin:
        raise SystemExit("The origin does not match the assigned loopback port of the source QA project.")
    if docker("volume", "ls", "--filter", f"label=com.docker.compose.project={target}", "-q").strip():
        raise SystemExit("Restore target already has a volume. Use a fresh QA project.")
    tables = TABLES + EVALUATION_TABLES if restore_origin else TABLES
    if restore_origin:
        with httpx2.Client(base_url=origin + "/api/v1", timeout=20, trust_env=False) as client:
            training = evaluation_fixture(client, origin, wait_timeout)
            compose(project, "stop", "worker", development=True)
            try:
                waiting = new_training_run(client)
                finish_training(client, waiting)
                change_lease(project, waiting)
            except BaseException:
                compose(project, "start", "worker", development=True)
                raise
    else:
        ensure_simulation_fixture(origin)
    compose(project, "stop", "api", "worker", development=True)
    try:
        before = fingerprint(project, tables)
        if any(before[table][0] == 0 for table in tables):
            raise RuntimeError("Run the QA workflow with indexed SOPs before taking a backup.")
        dump = compose(project, "exec", "-T", "db", "pg_dump", "-U", "postgres", "-d", "polytech", "-Fc")
        archive = sop_archive(project, development=True)
    finally:
        compose(project, "start", "api", "worker", development=True)
    files = file_digests(archive)
    if not files:
        raise RuntimeError("The QA SOP archive must contain original files.")

    compose(project, "restart", "db", "api", "worker", "frontend", "nginx", development=True)
    wait_for_stack(project, wait_timeout)
    if before != fingerprint(project, tables) or files != file_digests(sop_archive(project, development=True)):
        raise RuntimeError("QA data changed during container restart.")

    with httpx2.Client(base_url=origin, timeout=20, trust_env=False) as client:
        operator_login = client.post("/api/v1/auth/login", headers={"Origin": origin}, json={
            "username": USERNAMES[0], "password": PASSWORD,
        })
        operator_login.raise_for_status()
        marker = f"qa-private-search-{uuid4()}"
        search = client.get("/api/v1/tickets", params={"q": marker})
        search.raise_for_status()
        logs = compose(project, "logs", "--since", "5m", "--no-color", "api", "nginx", development=True)
        if marker.encode() in logs or search.headers["x-request-id"].encode() not in logs:
            raise RuntimeError("Expected a correlated access log without the search query.")
        compose(project, "stop", "db", development=True)
        try:
            outage = client.get("/api/v1/tickets")
            if outage.status_code != 503 or outage.json()["error"]["code"] != "DB_UNAVAILABLE":
                raise RuntimeError("Expected the sanitized DB outage response.")
            health = client.get("/health")
            if health.status_code != 200 or health.json() != {"status": "ok"}:
                raise RuntimeError("Liveness contract changed during the DB outage.")
            status, readiness = api_readiness(project)
            if status != 503 or readiness["error"]["code"] != "DB_NOT_READY":
                raise RuntimeError("The internal API reported ready while the DB was stopped.")
            outage_login = client.post("/api/v1/auth/login", headers={"Origin": origin}, json={
                "username": USERNAMES[0], "password": PASSWORD,
            })
            if outage_login.status_code != 503 or outage_login.json()["error"]["code"] != "DB_UNAVAILABLE":
                raise RuntimeError("Expected a sanitized DB outage response on login.")
            identifier = outage_login.headers["x-request-id"].encode()
            for service in ("api", "nginx"):
                logs = compose(project, "logs", "--since", "2m", "--no-color", service, development=True)
                if identifier not in logs:
                    raise RuntimeError(f"The synthetic login outage was missing from the {service} request log.")
        finally:
            wait_for_stack(project, wait_timeout)
        client.get("/api/v1/tickets").raise_for_status()

    # The same Compose image and role initialization are used with separate named volumes.
    compose(target, "up", "--wait", "--wait-timeout", str(wait_timeout), "db")
    compose(target, "exec", "-T", "db", "pg_restore", "--exit-on-error", "--single-transaction",
            "-U", "postgres", "-d", "polytech", input_data=dump)
    compose(target, "build", "worker")
    compose(target, "run", "--rm", "--no-deps", "-T", "worker",
            "tar", "-C", "/var/lib/polytech/sops", "-xf", "-", input_data=archive)
    if before != fingerprint(target, tables) or files != file_digests(sop_archive(target)):
        raise RuntimeError("Restored database rows or SOP originals did not match the backup.")
    result = {"restart": "passed", "db_outage_and_recovery": "passed", "controlled_login_503": "passed",
              "restore": "passed", "access_log": "passed",
              "tables": {table: count for table, (count, _) in before.items()}, "sop_files": len(files)}
    if restore_origin:
        username, password, run, ticket, receipt, views = training
        os.environ.update(APP_ORIGIN=restore_origin, APP_PORT=restore_origin.rsplit(":", 1)[1], APP_TRAINING_REGISTRATION="0")
        change_lease(target, waiting, expired=True)
        compose(target, "up", "--build", "--wait", "--wait-timeout", str(wait_timeout), "nginx", "worker", development=True)
        with httpx2.Client(base_url=restore_origin + "/api/v1", timeout=20, trust_env=False) as client:
            login(client, restore_origin, username, password)
            if evidence_views(client, run, ticket) != views or not any(
                entry["id"] == run.rsplit("/", 1)[1] and entry["status"] == "COMPLETED"
                for entry in json_get(client, "/simulation-runs")["items"]
            ):
                raise RuntimeError("Restored history or completed evaluation differs from the source API.")
            key, body, expected = receipt
            replay = client.post(run + "/outgoing-handover/finalize", headers={"Idempotency-Key": key}, json=body)
            replay.raise_for_status()
            if replay.json() != expected:
                raise RuntimeError("Restored finalization did not replay its original response.")
            wait_for_evaluation(client, waiting, wait_timeout)
            if json_get(client, "/auth/registration") != {"enabled": False}:
                raise RuntimeError("External training signup must be disabled on the restore stack.")
        result.update(restored_completed_api="passed", restored_idempotent_receipt="passed", restored_expired_lease="passed")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--restore-project", required=True)
    parser.add_argument("--origin", default="http://127.0.0.1:18085")
    parser.add_argument("--restore-origin", help="Separate loopback port for complete Phase 5 training restore checks")
    parser.add_argument("--wait-timeout", type=int, default=240, help="Compose health and readiness timeout in seconds.")
    args = parser.parse_args()
    if args.wait_timeout <= 0:
        parser.error("--wait-timeout must be positive.")
    if (not all(re.fullmatch(r"polytech-qa-[a-z0-9-]+", name) for name in (args.project, args.restore_project))
            or args.project == args.restore_project or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", args.origin)
            or args.origin.endswith((":80", ":8080"))):
        parser.error("Use two distinct QA project names and the isolated loopback origin.")
    if args.restore_origin and (not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", args.restore_origin)
                                or args.restore_origin.endswith((":80", ":8080")) or args.restore_origin == args.origin):
        parser.error("Use a second, distinct QA loopback origin for restore API verification.")
    os.environ.update(APP_ORIGIN=args.origin, APP_PORT=args.origin.rsplit(":", 1)[1], AGENT_PROVIDER="fake",
                      GEMINI_API_KEY="", QA_PERSISTENCE_SOURCE=args.project)
    if args.restore_origin:
        os.environ["APP_TRAINING_REGISTRATION"] = "1"
    print(json.dumps(verify(args.project, args.restore_project, args.origin, args.wait_timeout, args.restore_origin), indent=2))
