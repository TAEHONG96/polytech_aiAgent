"""Correlate the optional benchmark report with Nginx and QA API timing logs."""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import median, quantiles


def summary(report, nginx_log, api_log):
    nginx_pattern = re.compile(
        r"request_id=([^\s]+) request_time=([\d.]+) upstream_time=([\d.]+) upstream_connect=([\d.]+)"
    )
    nginx = {identifier: (float(total) * 1000, float(upstream) * 1000, float(connect) * 1000)
             for identifier, total, upstream, connect in nginx_pattern.findall(nginx_log)}
    api = {}
    for line in api_log.splitlines():
        if "QA_TIMING " in line:
            entry = json.loads(line.split("QA_TIMING ", 1)[1])
            api[entry["request_id"]] = entry

    operations = defaultdict(lambda: defaultdict(list))
    missing = defaultdict(int)
    for sample in report["request_samples"]:
        operation, identifier = sample["operation"], sample["request_id"]
        if identifier not in nginx or identifier not in api:
            missing[operation] += 1
            continue
        total, upstream, connect = nginx[identifier]
        row = operations[operation]
        for metric, value in {
            "client_ms": sample["http_ms"], "nginx_ms": total,
            "client_outside_nginx_ms": max(0, sample["http_ms"] - total),
            "upstream_ms": upstream, "upstream_connect_ms": connect,
            "nginx_outside_upstream_ms": max(0, total - upstream),
        }.items():
            row[metric].append(value)
        for metric in ("fastapi_ms", "sql_ms", "pool_ms", "embedding_worker_ms", "sql_count"):
            row[metric].append(api[identifier][metric])

    output = {operation: {metric: {
        "n": len(values), "p50": round(median(values), 3),
        "p95": round(quantiles(values, n=20, method="inclusive")[18], 3) if len(values) > 1 else None,
    } for metric, values in row.items()} for operation, row in operations.items()}
    return {"operations": output, "uncorrelated_requests": dict(missing)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("nginx_log", type=Path)
    parser.add_argument("api_log", type=Path)
    args = parser.parse_args()
    report = summary(json.loads(args.benchmark.read_text(encoding="utf-8-sig")),
                     args.nginx_log.read_text(encoding="utf-8-sig"),
                     args.api_log.read_text(encoding="utf-8-sig"))
    print(json.dumps(report, indent=2))
    if report["uncorrelated_requests"]:
        raise SystemExit(1)
