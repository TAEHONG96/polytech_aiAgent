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
)


def docker(*args, input_data=None):
    executable = os.environ.get("QA_DOCKER_EXECUTABLE", "docker")
    return subprocess.run([executable, *args], input=input_data, stdout=subprocess.PIPE, check=True).stdout


def compose(project, *args, development=False, input_data=None):
    files = ["-f", "compose.yaml"]
    if development:
        files.extend(["-f", "compose.override.yaml"])
    return docker("compose", "-p", project, *files, *args, input_data=input_data)


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


def verify(project, target, origin):
    if docker("volume", "ls", "--filter", f"label=com.docker.compose.project={target}", "-q").strip():
        raise SystemExit("Restore target already has a volume. Use a fresh QA project.")
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
    compose(project, "up", "--no-recreate", "--wait", "--wait-timeout", "60", "nginx", "worker", development=True)
    if before != fingerprint(project) or files != file_digests(sop_archive(project, development=True)):
        raise RuntimeError("QA data changed during container restart.")

    with httpx2.Client(base_url=origin, timeout=12, trust_env=False) as client:
        login = client.post("/api/v1/auth/login", headers={"Origin": origin}, json={
            "username": USERNAMES[0], "password": PASSWORD,
        })
        login.raise_for_status()
        marker = f"qa-private-search-{uuid4()}"
        search = client.get("/api/v1/tickets", params={"q": marker})
        search.raise_for_status()
        logs = compose(project, "logs", "--since", "1m", "--no-color", "api", "nginx", development=True)
        if marker.encode() in logs or search.headers["x-request-id"].encode() not in logs:
            raise RuntimeError("Expected a correlated access log without the search query.")
        compose(project, "stop", "db", development=True)
        try:
            outage = client.get("/api/v1/tickets")
            if outage.status_code != 503 or outage.json()["error"]["code"] != "DB_UNAVAILABLE":
                raise RuntimeError("Expected the sanitized DB outage response.")
            if client.get("/health").json() != {"status": "ok"}:
                raise RuntimeError("Liveness contract changed during the DB outage.")
        finally:
            compose(project, "up", "--no-recreate", "--wait", "--wait-timeout", "60", "nginx", "worker",
                    development=True)
        client.get("/api/v1/tickets").raise_for_status()

    # The same Compose image and role initialization are used with separate named volumes.
    compose(target, "up", "--wait", "db")
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
    args = parser.parse_args()
    if (not all(re.fullmatch(r"polytech-qa-[a-z0-9-]+", name) for name in (args.project, args.restore_project))
            or args.project == args.restore_project or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", args.origin)
            or args.origin.endswith((":80", ":8080"))):
        parser.error("Use two distinct QA project names and the isolated loopback origin.")
    os.environ.update(APP_ORIGIN=args.origin, APP_PORT=args.origin.rsplit(":", 1)[1], AGENT_PROVIDER="fake",
                      GEMINI_API_KEY="")
    print(json.dumps(verify(args.project, args.restore_project, args.origin), indent=2))
