"""Measure the HTTP API through Nginx on a synthetic Fake QA stack."""

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import httpx2

from tests.qa_roster import PASSWORD, USERNAMES


def measure(base_url: str, shifts: list[str], *, profile: bool = False) -> dict:
    timings = defaultdict(list)
    statuses = defaultdict(Counter)
    expected_statuses = {}
    request_samples = []

    def checked(response, expected):
        if response.status_code != expected:
            raise RuntimeError(f"{response.request.method} {response.request.url.path}: {response.status_code}, expected {expected}")
        return response.json()

    def login(client, username):
        response = client.post("/auth/login", headers={"Origin": base_url},
                               json={"username": username, "password": PASSWORD})
        client.headers.update({"Origin": base_url, "X-CSRF-Token": checked(response, 200)["csrf_token"]})

    def recorded(client, name, method, path, expected=200, **kwargs):
        if profile:
            kwargs["headers"] = {"X-QA-Operation": name}
        start = time.perf_counter()
        response = client.request(method, path, **kwargs)
        elapsed_ms = (time.perf_counter() - start) * 1000
        timings[name].append(elapsed_ms)
        statuses[name][response.status_code] += 1
        expected_statuses[name] = expected
        if profile:
            request_samples.append({"operation": name, "request_id": response.headers.get("x-request-id"),
                                    "http_ms": round(elapsed_ms, 2)})
        return response

    options = dict(base_url=base_url + "/api/v1", timeout=20, trust_env=False)
    with httpx2.Client(**options) as outgoing, httpx2.Client(**options) as incoming:
        login(outgoing, USERNAMES[0])
        login(incoming, USERNAMES[1])
        clients = (outgoing, incoming)
        search = "서버 부팅 실패와 메인보드 오류 POST B101"
        now = datetime.now(timezone.utc).isoformat()
        marker = uuid4().hex[:8]
        tickets = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            for response in pool.map(lambda index: recorded(
                outgoing, "ticket_create", "POST", "/tickets", 201, json={
                    "title": f"Synthetic QA {marker} {index}", "description": search,
                    "detected_at": now, "service": "synthetic", "severity": "P2",
                },
            ), range(20)):
                if response.status_code == 201:
                    tickets.append(response.json()["id"])
            list(pool.map(lambda ticket_id: recorded(outgoing, "ticket_get", "GET", f"/tickets/{ticket_id}"), tickets))
            for source in ("SOP", "TICKET"):
                list(pool.map(lambda _: recorded(outgoing, "search_" + source.lower(), "POST", "/knowledge/search",
                                                json={"query": search, "source_types": [source]}), range(20)))
        if len(tickets) < 20:
            raise RuntimeError(f"Ticket setup incomplete: {dict(statuses['ticket_create'])}")

        for _ in range(5):
            start = time.perf_counter()
            response = recorded(outgoing, "agent_enqueue", "POST", "/agent/analyze", 202,
                                json={"ticket_id": tickets[0], "message": search})
            run_id = checked(response, 202)["run_id"]
            while time.perf_counter() - start < 30:
                run = checked(outgoing.get(f"/agent/runs/{run_id}"), 200)
                if run["status"] in ("succeeded", "failed"):
                    timings["agent_completion"].append((time.perf_counter() - start) * 1000)
                    statuses["agent_completion"][run["status"]] += 1
                    expected_statuses["agent_completion"] = "succeeded"
                    if run["status"] != "succeeded" or run["provider"] != "fake":
                        raise RuntimeError("Expected a successful Fake agent in the QA stack.")
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Synthetic analysis did not finish within 30 seconds.")

        for index, size in enumerate((1, 5, 20)):
            shift, actor = shifts[index], clients[index % 2]
            preview = checked(actor.get(f"/shift-instances/{shift}/handover-preview"), 200)
            keep = set(tickets[:size])
            exclude = [item["id"] for item in preview["items"] if item["id"] not in keep]
            report = checked(recorded(actor, f"handover_create_{size}", "POST", "/handovers", 201,
                                      json={"outgoing_shift_id": shift, "exclude_ticket_ids": exclude}), 201)
            if len(report["items"]) != size:
                raise RuntimeError("Report size did not match the sample size.")
            path = f"/handovers/{report['id']}"
            ready = checked(actor.put(path, json={
                "expected_version": report["version"], "status": "READY",
                "body": {"summary": "합성 성능 검증", "gap_responses": [
                    {"gap_id": gap["id"], "answer": "합성 확인 대상"} for item in report["items"] for gap in item["gaps"]
                ]},
            }), 200)
            checked(recorded(actor, "handover_confirm", "POST", path + "/confirm",
                             json={"expected_version": ready["version"]}), 200)
            recorded(actor, "handover_get", "GET", path)

    result = {
        "provider": "fake", "max_concurrency": 5, "agent_poll_interval_ms": 100,
        "operations": {name: {
            "count": len(samples), "statuses": dict(statuses[name]),
            "errors": len(samples) - statuses[name][expected_statuses[name]],
            "p50_ms": round(statistics.median(samples), 2),
            "p95_ms": round(statistics.quantiles(samples, n=20, method="inclusive")[18], 2) if len(samples) > 1 else None,
            "max_ms": round(max(samples), 2),
        } for name, samples in timings.items()},
    }
    if profile:
        result["request_samples"] = request_samples
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:18085")
    parser.add_argument("--shifts", nargs=3, required=True)
    parser.add_argument("--profile", action="store_true", help="Include request IDs for QA timing correlation.")
    args = parser.parse_args()
    origin = httpx2.URL(args.origin)
    if origin.scheme != "http" or origin.host != "127.0.0.1" or origin.port in {80, 8080, None} or origin.path != "/":
        parser.error("Use the loopback origin of the isolated Fake QA stack.")
    report = measure(args.origin, args.shifts, profile=args.profile)
    print(json.dumps(report, indent=2))
    if any(operation["errors"] for operation in report["operations"].values()):
        raise SystemExit(1)
