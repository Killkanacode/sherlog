"""Inject each scenario and check that it shows up in telemetry.

For every scenario: reset, settle, snapshot baseline signals, inject, wait,
snapshot again, print the deltas, reset. Uses only the standard library so it
runs on the host:

    python3 chaos/verify_scenarios.py [scenario-id-prefix ...]
"""

import json
import sys
import time
import urllib.parse
import urllib.request

CHAOS = "http://localhost:8100"
PROM = "http://localhost:9090"
LOKI = "http://localhost:3100"
PLATFORM = "http://localhost:8050"

SETTLE_S = 45
OBSERVE_S = {"11-inventory-memleak": 300}
DEFAULT_OBSERVE_S = 90


def call(method: str, url: str, body: dict | None = None, timeout: float = 300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def prom(query: str) -> list[dict]:
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    return call("GET", url)["data"]["result"]


def loki(query: str) -> list[dict]:
    url = f"{LOKI}/loki/api/v1/query?" + urllib.parse.urlencode({"query": query})
    return call("GET", url)["data"]["result"]


def snapshot() -> dict:
    window = "1m"
    snap = {"rps": {}, "err": {}, "p95": {}, "client": {}, "log_errors": {}, "rss_mb": {}, "cpu": {}}
    for r in prom(f"sum by (job) (rate(http_server_duration_seconds_count[{window}]))"):
        snap["rps"][r["metric"]["job"]] = float(r["value"][1])
    for r in prom(
        f'sum by (job) (rate(http_server_duration_seconds_count{{status_code=~"5.."}}[{window}]))'
        f" / sum by (job) (rate(http_server_duration_seconds_count[{window}]))"
    ):
        snap["err"][r["metric"]["job"]] = float(r["value"][1])
    for r in prom(f"histogram_quantile(0.95, sum by (job, le) (rate(http_server_duration_seconds_bucket[{window}])))"):
        snap["p95"][r["metric"]["job"]] = float(r["value"][1])
    for r in prom(f"sum by (job, peer, outcome) (rate(http_client_duration_seconds_count[{window}]))"):
        m = r["metric"]
        snap["client"][f"{m['job']}->{m['peer']}:{m['outcome']}"] = float(r["value"][1])
    for r in loki(f'sum by (service_name) (count_over_time({{service_name=~".+"}} | severity_text="ERROR" [{window}]))'):
        snap["log_errors"][r["metric"]["service_name"]] = float(r["value"][1])
    for r in prom("max by (job) (process_memory_rss_bytes)"):
        snap["rss_mb"][r["metric"]["job"]] = float(r["value"][1]) / 1e6
    for r in prom(f"sum by (job) (rate(process_cpu_time_seconds_total[{window}]))"):
        snap["cpu"][r["metric"]["job"]] = float(r["value"][1])
    snap["containers"] = {
        s["service"]: [(c["status"], c["health"], c["restart_count"], c["oom_killed"]) for c in s["containers"]]
        for s in call("GET", f"{PLATFORM}/services")
    }
    return snap


def fmt(value: float | None, kind: str) -> str:
    if value is None:
        return "-"
    if kind == "err":
        return f"{value:.0%}"
    if kind == "p95":
        return f"{value * 1000:.0f}ms" if value == value else "nan"
    if kind in ("rss_mb",):
        return f"{value:.0f}"
    return f"{value:.2f}"


def report(before: dict, after: dict) -> None:
    for kind in ("rps", "err", "p95", "cpu", "rss_mb", "log_errors"):
        jobs = sorted(set(before[kind]) | set(after[kind]))
        cells = [f"{j} {fmt(before[kind].get(j), kind)}→{fmt(after[kind].get(j), kind)}" for j in jobs]
        print(f"    {kind:10} " + " | ".join(cells))
    changed = {
        k: (before["client"].get(k, 0), v)
        for k, v in after["client"].items()
        if not k.endswith(":success") and v > 0.01
    }
    print("    client     " + (" | ".join(f"{k} {a:.2f}→{b:.2f}" for k, (a, b) in sorted(changed.items())) or "no failing calls"))
    unusual = {
        service: states
        for service, states in after["containers"].items()
        if any(status != "running" or health not in ("healthy", None) or restarts or oom for status, health, restarts, oom in states)
    }
    print(f"    containers {unusual or 'all running and healthy'}")


def main(prefixes: list[str]) -> None:
    scenarios = call("GET", f"{CHAOS}/scenarios")
    if prefixes:
        scenarios = [s for s in scenarios if any(s["id"].startswith(p) for p in prefixes)]
    for scenario in scenarios:
        sid = scenario["id"]
        print(f"\n=== {sid}: {scenario['title']}", flush=True)
        call("POST", f"{CHAOS}/reset")
        time.sleep(SETTLE_S)
        before = snapshot()
        started = call("POST", f"{CHAOS}/scenarios/{sid}/start")
        delay = max([s.get("delay_s", 0) for s in scenario["inject"]] + [0])
        print(f"    run {started['run_id']} injected {len(started['injected'])}, scheduled {started['scheduled']}", flush=True)
        time.sleep(delay + OBSERVE_S.get(sid, DEFAULT_OBSERVE_S))
        after = snapshot()
        report(before, after)
        events = call("GET", f"{PLATFORM}/events?limit=3")
        recent = [f"{e['type']} {e['service'] or e['details'].get('flag')} by {e['actor']}" for e in events if e["actor"] != "ops"]
        print(f"    events     {recent[-2:]}", flush=True)
    call("POST", f"{CHAOS}/reset")
    print("\nDone; system reset to baseline.")


if __name__ == "__main__":
    main(sys.argv[1:])
