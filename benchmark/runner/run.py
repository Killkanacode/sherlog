"""Benchmark runner.

For each scenario: reset -> wait for a quiet, healthy system -> inject ->
wait for Sherlog to open an incident -> observe -> score -> reset. A fault-free
baseline run counts false positives. Results go to benchmark/results/.

Talks to the chaos controller (for ground truth and injection) and to
Sherlog's public API, like the UI does. Standard library only:

    python3 benchmark/runner/run.py                     # all scenarios + baseline
    python3 benchmark/runner/run.py 03 05               # scenarios by id prefix
    python3 benchmark/runner/run.py --baseline-only --baseline-minutes 10
"""

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CHAOS = "http://localhost:8100"
SHERLOG = "http://localhost:8200"
RESULTS = Path(__file__).resolve().parent.parent / "results"

QUIET_S = 30  # healthy and incident-free for this long before injecting
QUIET_TIMEOUT_S = 420
OBSERVE_AFTER_DETECT_S = 90


def call(method: str, url: str, body: dict | None = None, timeout: float = 300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def wait_quiet() -> bool:
    """Wait until Sherlog has no open incidents and no active impact signals."""
    deadline = time.monotonic() + QUIET_TIMEOUT_S
    quiet_since = None
    while time.monotonic() < deadline:
        open_incidents = call("GET", f"{SHERLOG}/incidents?status=open")
        signals = [s for s in call("GET", f"{SHERLOG}/signals") if s["details"].get("impact", True)]
        if not open_incidents and not signals:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= QUIET_S:
                return True
        else:
            quiet_since = None
        time.sleep(5)
    return False


def incidents_since(t0: datetime) -> list[dict]:
    return [i for i in call("GET", f"{SHERLOG}/incidents") if parse(i["created_at"]) >= t0]


def run_scenario(scenario: dict) -> dict:
    sid, truth = scenario["id"], scenario["ground_truth"]
    accepted = set(truth.get("services_accepted", [truth["service"]]))
    detect_timeout = scenario.get("timeouts", {}).get("detect_s", 120)

    call("POST", f"{CHAOS}/reset")
    if not wait_quiet():
        log(f"{sid}: system never got quiet; running anyway")

    t0 = now()
    run = call("POST", f"{CHAOS}/scenarios/{sid}/start")
    log(f"{sid}: injected ({run['run_id']})")

    detected_at, first = None, None
    deadline = time.monotonic() + detect_timeout + max([s.get("delay_s", 0) for s in scenario["inject"]] + [0])
    while time.monotonic() < deadline:
        found = incidents_since(t0)
        if found:
            first = min(found, key=lambda i: i["id"])
            detected_at = parse(first["created_at"])
            break
        time.sleep(3)

    result = {
        "scenario": sid,
        "title": scenario["title"],
        "truth_service": truth["service"],
        "truth_category": truth["category"],
        "detected": detected_at is not None,
        "ttd_s": round((detected_at - t0).total_seconds(), 1) if detected_at else None,
    }
    if first is None:
        log(f"{sid}: NOT detected within {detect_timeout}s")
        call("POST", f"{CHAOS}/reset")
        return {**result, "incidents": 0}

    time.sleep(OBSERVE_AFTER_DETECT_S)
    incidents = incidents_since(t0)
    detail = call("GET", f"{SHERLOG}/incidents/{first['id']}")
    evidence = detail.get("evidence") or call("POST", f"{SHERLOG}/incidents/{first['id']}/evidence")
    hint = (evidence.get("root_cause_hint") or {}).get("service")
    stats = evidence.get("stats", {})
    result.update(
        {
            "incidents": len(incidents),
            "incident_id": first["id"],
            "incident_services": detail["services"],
            "signal_types": sorted({s["type"] for s in detail["signals"]}),
            "hint_service": hint,
            "hint_correct": hint in accepted,
            "truth_in_incident": bool(accepted & set(detail["services"])) or hint in accepted,
            "log_lines": stats.get("log_lines_in_window"),
            "evidence_tokens": stats.get("estimated_tokens"),
        }
    )
    log(
        f"{sid}: incident #{first['id']} after {result['ttd_s']}s, {len(incidents)} incident(s), "
        f"services {detail['services']}, hint {hint} ({'ok' if result['hint_correct'] else 'wrong'})"
    )
    call("POST", f"{CHAOS}/reset")
    return result


def run_baseline(minutes: float) -> dict:
    call("POST", f"{CHAOS}/reset")
    wait_quiet()
    t0 = now()
    log(f"baseline: watching {minutes} min without faults")
    time.sleep(minutes * 60)
    incidents = incidents_since(t0)
    for incident in incidents:
        log(f"baseline: false positive #{incident['id']} {incident['title']}")
    return {"minutes": minutes, "false_positives": len(incidents), "incidents": [i["title"] for i in incidents]}


def table(results: list[dict]) -> str:
    rows = [
        "| Scenario | Truth | Detected | TTD (s) | Incidents | Hint | Log lines → tokens |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        rows.append(
            f"| {r['title']} | {r['truth_service']} / {r['truth_category']} | {'yes' if r['detected'] else 'no'} | "
            f"{r['ttd_s'] if r['ttd_s'] is not None else '-'} | {r.get('incidents', 0)} | "
            f"{r.get('hint_service') or '-'} {'✓' if r.get('hint_correct') else '✗'} | "
            f"{r.get('log_lines') or '-'} → {r.get('evidence_tokens') or '-'} |"
        )
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prefixes", nargs="*")
    parser.add_argument("--baseline-minutes", type=float, default=10)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--no-baseline", action="store_true")
    args = parser.parse_args()

    results, baseline = [], None
    if not args.baseline_only:
        scenarios = call("GET", f"{CHAOS}/scenarios")
        if args.prefixes:
            scenarios = [s for s in scenarios if any(s["id"].startswith(p) for p in args.prefixes)]
        for scenario in scenarios:
            results.append(run_scenario(scenario))
    if args.baseline_only or not args.no_baseline:
        baseline = run_baseline(args.baseline_minutes)
    call("POST", f"{CHAOS}/reset")

    summary = {
        "detected": sum(r["detected"] for r in results),
        "exactly_one_incident": sum(r.get("incidents") == 1 for r in results),
        "hint_correct": sum(bool(r.get("hint_correct")) for r in results),
        "scenarios": len(results),
        "median_ttd_s": sorted(r["ttd_s"] for r in results if r["ttd_s"] is not None)[len([r for r in results if r["ttd_s"] is not None]) // 2]
        if any(r["ttd_s"] is not None for r in results) else None,
        "baseline": baseline,
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{now():%Y-%m-%dT%H%M}-detection.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print("\n" + table(results))
    print("\n" + json.dumps(summary, indent=2))
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
