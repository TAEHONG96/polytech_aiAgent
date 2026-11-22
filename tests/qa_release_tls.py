"""Check the production proxy and auth policy on a loopback-only TLS staging stack."""
import argparse
import json
import os
import ssl
from urllib.parse import urlsplit

import httpx2


def check(origin, ca, username, password):
    with httpx2.Client(base_url=origin, verify=ssl.create_default_context(cafile=ca), timeout=20, trust_env=False) as client:
        health = client.get("/health")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert client.get("/api/v1/ready").status_code == 404
        assert client.get("/api/v1/auth/registration").json() == {"enabled": False}
        assert client.post("/api/v1/auth/register", headers={"Origin": origin}, json={}).status_code == 404
        credentials = {"username": username, "password": password}
        assert client.post("/api/v1/auth/login", json=credentials).status_code == 403
        login = client.post("/api/v1/auth/login", headers={"Origin": origin}, json=credentials)
        assert login.status_code == 200
        policy = login.headers["Set-Cookie"].split(";")[1:]
        assert {"secure", "httponly", "samesite=lax"} <= {item.strip().lower() for item in policy}
        assert login.headers["Cache-Control"] == "no-store"
        assert client.get("/api/v1/auth/session").status_code == 200
        assert client.post("/api/v1/auth/logout", headers={"Origin": origin}).status_code == 403
        assert client.post("/api/v1/auth/logout", headers={"Origin": origin,
                           "X-CSRF-Token": login.json()["csrf_token"]}).status_code == 204
        assert client.get("/api/v1/auth/session").status_code == 401
    return {"tls_with_ca": "passed", "health_and_private_readiness": "passed", "public_signup_disabled": "passed",
            "secure_session_cookie": "passed", "origin_and_csrf": "passed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://127.0.0.1:18443")
    parser.add_argument("--ca", required=True)
    args = parser.parse_args()
    url = urlsplit(args.origin)
    if url.scheme != "https" or url.hostname != "127.0.0.1" or url.port in {None, 443} or url.path or url.query or url.fragment:
        parser.error("Use a loopback HTTPS staging origin on a separate port.")
    print(json.dumps(check(args.origin, args.ca, os.environ["QA_TLS_OPERATOR"], os.environ["QA_TLS_PASSWORD"])))
