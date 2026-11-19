"""Submit one synthetic incident to an isolated Gemini QA worker through Nginx."""

import argparse
import json
import time
from datetime import datetime, timezone

import httpx2

from tests.qa_roster import PASSWORD, USERNAMES


def smoke(origin):
    with httpx2.Client(base_url=origin + "/api/v1", timeout=12, trust_env=False) as client:
        login = client.post("/auth/login", headers={"Origin": origin},
                            json={"username": USERNAMES[0], "password": PASSWORD})
        login.raise_for_status()
        client.headers.update({"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]})
        incident = client.post("/tickets", json={
            "title": "합성 POST B101 장애",
            "description": "합성 서버가 POST 중 멈추고 모의 보드 진단 코드 B101을 표시했습니다.",
            "service": "synthetic-board", "severity": "P2", "detected_at": datetime.now(timezone.utc).isoformat(),
        })
        incident.raise_for_status()
        ticket_id = incident.json()["id"]
        started = time.monotonic()
        accepted = client.post("/agent/analyze", json={
            "ticket_id": ticket_id,
            "message": "합성 POST B101 장애입니다. 티켓 및 이력을 확인하고 가능한 경우 가상 SOP와 과거 사례를 비교해 주세요.",
        })
        accepted.raise_for_status()
        location = accepted.headers["location"].removeprefix("/api/v1")
        while time.monotonic() - started < 130:
            response = client.get(location)
            response.raise_for_status()
            run = response.json()
            if run["status"] in {"failed", "succeeded"}:
                evidence = (run["result"] or {}).get("evidence", [])
                summary = {field: run[field] for field in
                           ("status", "provider", "model", "usage", "tool_calls", "error")}
                summary.update(evidence_count=len(evidence),
                               current_ticket_cited=any(entry["source_id"] == ticket_id for entry in evidence),
                               elapsed_ms=round((time.monotonic() - started) * 1000, 2))
                print(json.dumps(summary, ensure_ascii=False))
                return int(run["status"] != "succeeded" or run["provider"] != "gemini"
                           or not any(call["status"] == "ok" for call in run["tool_calls"])
                           or not evidence)
            time.sleep(1)
    print(json.dumps({"status": "poll_timeout"}))
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:18089")
    args = parser.parse_args()
    url = httpx2.URL(args.origin)
    if url.scheme != "http" or url.host != "127.0.0.1" or url.port in {80, 8080, None} or url.path != "/":
        parser.error("Use the loopback origin of the isolated Gemini QA stack.")
    raise SystemExit(smoke(args.origin))
