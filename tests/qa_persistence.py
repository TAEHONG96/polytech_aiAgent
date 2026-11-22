"""Check restart, database outage, and restore into a fresh synthetic Compose project."""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from uuid import uuid4

import httpx2

from tests.qa_roster import PASSWORD, USERNAMES

TABLES = (
    "alembic_version", "tickets", "ticket_events", "ticket_work", "handovers", "handover_items",
    "handover_revisions", "sop_documents", "index_jobs", "knowledge_chunks", "agent_runs", "agent_tool_calls",
    "simulation_scenarios", "scenario_shift_slots", "scenario_hosts", "scenario_ticket_templates",
    "simulation_runs", "simulation_run_hosts",
)


def docker(*args, input_data=None):
    executable = os.environ.get("QA_DOCKER_EXECUTABLE", "docker")
    return subprocess.run([executable, *args], input=input_data, stdout=subprocess.PIPE, check=True).stdout


def compose(project, *args, development=False, input_data=None):
    files = ["-f", "compose.yaml"]
    if development:
        files.extend(["-f", os.environ.get("QA_COMPOSE_OVERRIDE", "compose.override.yaml")])
    return docker("compose", "--env-file", ".env.example", "-p", project, *files, *args, input_data=input_data)


def fingerprint(project):
    sql = " UNION ALL ".join(
        f"SELECT '{table}', count(*), md5(string_agg(serialized, '\n' ORDER BY serialized)) "
        f"FROM (SELECT to_jsonb(entry)::text AS serialized FROM {table} AS entry) AS encoded"
        for table in TABLES
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
        login = client.post("/api/v1/auth/login", headers={"Origin": origin}, json={
            "username": USERNAMES[0], "password": PASSWORD,
        })
        login.raise_for_status()
        state = client.get("/api/v1/simulation")
        state.raise_for_status()
        if not state.json()["active_run_id"]:
            response = client.post("/api/v1/simulation-runs", headers={
                "Origin": origin, "X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": "qa-persistence-start",
            }, json={"scenario_key": "dco-basic", "scenario_version": "1.0"})
            response.raise_for_status()


def verify(project, target, origin, wait_timeout):
    published = compose(project, "port", "nginx", "80", development=True).decode().strip()
    if f"http://{published}" != origin:
        raise SystemExit("The origin does not match the assigned loopback port of the source QA project.")
    if docker("volume", "ls", "--filter", f"label=com.docker.compose.project={target}", "-q").strip():
        raise SystemExit("Restore target already has a volume. Use a fresh QA project.")
    ensure_simulation_fixture(origin)
    compose(project, "stop", "api", "worker", development=True)
    try:
        before = fingerprint(project)
        if any(before[table][0] == 0 for table in TABLES):
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
    if before != fingerprint(project) or files != file_digests(sop_archive(project, development=True)):
        raise RuntimeError("QA data changed during container restart.")

    with httpx2.Client(base_url=origin, timeout=20, trust_env=False) as client:
        login = client.post("/api/v1/auth/login", headers={"Origin": origin}, json={
            "username": USERNAMES[0], "password": PASSWORD,
        })
        login.raise_for_status()
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
    if before != fingerprint(target) or files != file_digests(sop_archive(target)):
        raise RuntimeError("Restored database rows or SOP originals did not match the backup.")
    return {"restart": "passed", "db_outage_and_recovery": "passed", "restore": "passed", "access_log": "passed",
            "tables": {table: count for table, (count, _) in before.items()}, "sop_files": len(files)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--restore-project", required=True)
    parser.add_argument("--origin", default="http://127.0.0.1:18085")
    parser.add_argument("--wait-timeout", type=int, default=240, help="Compose health and readiness timeout in seconds.")
    args = parser.parse_args()
    if args.wait_timeout <= 0:
        parser.error("--wait-timeout must be positive.")
    if (not all(re.fullmatch(r"polytech-qa-[a-z0-9-]+", name) for name in (args.project, args.restore_project))
            or args.project == args.restore_project or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", args.origin)
            or args.origin.endswith((":80", ":8080"))):
        parser.error("Use two distinct QA project names and the isolated loopback origin.")
    os.environ.update(APP_ORIGIN=args.origin, APP_PORT=args.origin.rsplit(":", 1)[1], AGENT_PROVIDER="fake",
                      GEMINI_API_KEY="")
    print(json.dumps(verify(args.project, args.restore_project, args.origin, args.wait_timeout), indent=2))
