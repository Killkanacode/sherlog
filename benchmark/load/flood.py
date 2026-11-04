"""Log flood: OTLP/HTTP log records into the collector at a fixed rate.

Mostly INFO access lines with a sprinkle of WARN, the shape of real service
logs. Runs inside the compose network (standard library only):

  docker run --rm --network sherlog_app -v "$PWD/benchmark/load:/load:ro" \
    python:3.12-slim python /load/flood.py --rate 5000 --seconds 150
"""

import argparse
import json
import random
import time
import urllib.request

ROUTES = ["/products/SKU-{n:07d}", "/products?page={n}", "/reserve/SKU-{n:07d}", "/stock/SKU-{n:07d}"]


def record(now_ns: int, warn: bool, message: str | None = None) -> dict:
    n = random.randint(1, 999_999)
    if message:
        body, level, number = message, "WARN", 13
    elif warn:
        body = f"Slow cache refresh for SKU-{n:07d} took {random.randint(200, 900)}ms"
        level, number = "WARN", 13
    else:
        route = random.choice(ROUTES).format(n=n)
        body = f"GET {route} 200 {random.uniform(0.5, 30):.1f}ms"
        level, number = "INFO", 9
    return {
        "timeUnixNano": str(now_ns),
        "severityNumber": number,
        "severityText": level,
        "body": {"stringValue": body},
        "attributes": [{"key": "logger", "value": {"stringValue": "loadtest"}}],
    }


def send(url: str, records: list[dict], service: str) -> None:
    payload = {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                "scopeLogs": [{"scope": {"name": "loadtest"}, "logRecords": records}],
            }
        ]
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    urllib.request.urlopen(request, timeout=10).read()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=int, default=5000, help="log lines per second")
    parser.add_argument("--seconds", type=int, default=150)
    parser.add_argument("--warn-share", type=float, default=0.001)
    parser.add_argument("--service", default="inventory")
    parser.add_argument("--url", default="http://otel-collector:4318/v1/logs")
    parser.add_argument("--message", help="send this WARN line every time instead of generated ones")
    args = parser.parse_args()

    batch = max(1, args.rate // 10)  # ten posts a second
    sent, started = 0, time.monotonic()
    while time.monotonic() - started < args.seconds:
        tick = time.monotonic()
        now_ns = time.time_ns()
        records = [record(now_ns + i, random.random() < args.warn_share, args.message) for i in range(batch)]
        send(args.url, records, args.service)
        sent += batch
        time.sleep(max(0.0, 0.1 - (time.monotonic() - tick)))
    elapsed = time.monotonic() - started
    print(json.dumps({"sent": sent, "seconds": round(elapsed, 1), "rate": round(sent / elapsed)}))


if __name__ == "__main__":
    main()
