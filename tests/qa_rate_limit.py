"""Exercise the local Nginx limiter from separate source IPs in its QA network namespace."""

import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from secrets import randbelow
from urllib.parse import urlsplit

from tests.qa_roster import PASSWORD, USERNAMES


def verify(origin: str) -> dict:
    prefix = f"127.{1 + randbelow(254)}.{1 + randbelow(254)}"
    sequential_ip, burst_ip, independent_ip = (f"{prefix}.{index}" for index in (1, 2, 3))

    def login(source: str, password: str = "synthetic-wrong-password") -> int:
        with closing(HTTPConnection("127.0.0.1", 80, timeout=25, source_address=(source, 0))) as connection:
            connection.request("POST", "/api/v1/auth/login",
                               body=json.dumps({"username": USERNAMES[0], "password": password}),
                               headers={"Origin": origin, "Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read()
            if response.status == 429:
                error = json.loads(body)["error"]
                assert error["code"] == "SIGNIN_RATE_LIMIT"
                assert error["request_id"] == response.getheader("X-Request-ID")
            return response.status

    sequential = [login(sequential_ip), login(sequential_ip, PASSWORD), login(sequential_ip)]
    assert sequential == [401, 200, 401], sequential

    with ThreadPoolExecutor(max_workers=18) as pool:
        attempts = Counter(pool.map(login, [burst_ip] * 18))
    assert attempts[401] and attempts[429] and set(attempts) == {401, 429}, attempts

    # The same username gets a separate bucket when the address changes.
    independent = login(independent_ip, PASSWORD)
    assert independent == 200, independent

    # At 12 requests/minute a saturated bucket admits a request after capacity drains.
    # Probe for admission so latency in the preceding burst does not define the deadline.
    deadline = time.monotonic() + 15
    while True:
        recovered = login(burst_ip)
        if recovered != 429 or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    assert recovered == 401, recovered
    return {"sequential": sequential, "burst": dict(attempts), "independent_ip_same_account": independent,
            "after_refill": recovered}


if __name__ == "__main__":
    origin = os.environ.get("APP_ORIGIN", "")
    url = urlsplit(origin)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.port in {None, 80, 8080}
            or origin != f"http://127.0.0.1:{url.port}"):
        raise SystemExit("Use the configured loopback origin of the isolated Nginx QA stack.")
    print(json.dumps(verify(origin)))
