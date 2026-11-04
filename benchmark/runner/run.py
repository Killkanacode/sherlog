"""Benchmark runner.

For each scenario: reset -> wait for a quiet, healthy system -> inject ->
wait for Sherlog to open an incident -> wait for its diagnosis -> score ->
reset. A fault-free baseline run counts false positives. Results go to
benchmark/results/. Runs against fake models (SHERLOG_FAKE_LLM) are labelled
as such: they test the plumbing, not the diagnosis.

Talks to the chaos controller (for ground truth and injection) and to
Sherlog's public API, like the UI does, with an operator token: SHERLOG_TOKEN,
or SHERLOG_ADMIN_TOKEN from .env. The benchmark scores the diagnosis, not the
fix: an action Sherlog proposes is recorded and turned down, and the chaos
reset repairs the fault. Standard library only:

    python3 benchmark/runner/run.py                     # all scenarios + baseline
    python3 benchmark/runner/run.py 03 05               # scenarios by id prefix
    python3 benchmark/runner/run.py --baseline-only --baseline-minutes 10
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CHAOS = "http://localhost:8100"
SHERLOG = "http://localhost:8200"
RESULTS = Path(__file__).resolve().parent.parent / "results"
ENV = Path(__file__).resolve().parents[2] / ".env"

QUIET_S = 30  # healthy and incident-free for this long before injecting
QUIET_TIMEOUT_S = 420
OBSERVE_AFTER_DETECT_S = 90
SHERLOG_INVESTIGATES = True  # read from Sherlog's /health at start
DIAGNOSIS_TIMEOUT_S = 300  # after detection


def token() -> str:
    if os.environ.get("SHERLOG_TOKEN"):
        return os.environ["SHERLOG_TOKEN"]
    for line in ENV.read_text().splitlines() if ENV.exists() else []:
        if line.startswith("SHERLOG_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


TOKEN = token()


def call(method: str, url: str, body: dict | None = None, timeout: float = 300):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if TOKEN and url.startswith(SHERLOG):
        headers["authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def median(values: list) -> float | None:
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def wait_quiet() -> bool:
    """Wait until Sherlog has learned its baselines and has no open incidents
    and no active impact signals."""
    deadline = time.monotonic() + QUIET_TIMEOUT_S
    quiet_since = None
    while time.monotonic() < deadline:
        if call("GET", f"{SHERLOG}/health").get("learning"):
            time.sleep(5)
            continue
        # An escalated incident is not "open" but still collects signals until
        # it is over, so wait for every incident to be resolved.
        open_incidents = [i for i in call("GET", f"{SHERLOG}/incidents") if not i["resolved_at"]]
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

    detail = wait_diagnosis(first["id"], detected_at)
    incidents = incidents_since(t0)
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
    result.update(score_diagnosis(detail, truth, accepted, detected_at))
    result.update(turn_down_action(first["id"], detail, truth))
    log(
        f"{sid}: incident #{first['id']} after {result['ttd_s']}s, {len(incidents)} incident(s), "
        f"services {detail['services']}, hint {hint} ({'ok' if result['hint_correct'] else 'wrong'})"
        + (f", diagnosis {result['diagnosis_service']}/{result['diagnosis_category']} "
           f"({'ok' if result['cause_correct'] else 'wrong'})" if result.get("diagnosed") else "")
        + (f", proposed {result['proposed_action']} ({'ok' if result['action_acceptable'] else 'wrong'})"
           if result.get("proposed_action") else "")
    )
    call("POST", f"{CHAOS}/reset")
    return result


def wait_diagnosis(incident_id: int, detected_at: datetime) -> dict:
    """Observe for a while (to count duplicate incidents), and until the
    investigation has finished if Sherlog is investigating."""
    deadline = time.monotonic() + DIAGNOSIS_TIMEOUT_S
    observe_until = time.monotonic() + OBSERVE_AFTER_DETECT_S
    while True:
        detail = call("GET", f"{SHERLOG}/incidents/{incident_id}")
        investigation = detail.get("investigation") or {}
        pending = investigation.get("state") == "running" or (
            not investigation and detail["status"] in ("detected", "investigating") and SHERLOG_INVESTIGATES
        )
        if time.monotonic() >= observe_until and not pending:
            return detail
        if time.monotonic() >= deadline:
            log(f"incident #{incident_id}: no diagnosis within {DIAGNOSIS_TIMEOUT_S}s")
            return detail
        time.sleep(3)


def score_diagnosis(detail: dict, truth: dict, accepted: set, detected_at: datetime) -> dict:
    investigation = detail.get("investigation") or {}
    diagnosis = detail.get("diagnosis")
    usage = detail.get("llm_usage") or {}
    if not diagnosis:
        return {"diagnosed": False, "investigation_state": investigation.get("state")}
    cause = diagnosis["root_cause"]
    categories = set(truth.get("categories_accepted", [truth["category"]]))
    service_ok, category_ok = cause["service"] in accepted, cause["category"] in categories
    finished = investigation.get("finished_at")
    return {
        "diagnosed": True,
        "investigation_state": investigation.get("state"),
        "llm_mode": investigation.get("mode"),
        "diagnosis_service": cause["service"],
        "diagnosis_category": cause["category"],
        "confidence": cause["confidence"],
        "recommended_action": diagnosis["recommended_action"]["type"],
        "service_correct": service_ok,
        "category_correct": category_ok,
        "cause_correct": service_ok and category_ok,
        "time_to_diagnosis_s": round((parse(finished) - detected_at).total_seconds(), 1) if finished else None,
        "llm_calls": usage.get("calls"),
        "llm_tokens": usage.get("total_tokens"),
        "llm_cost_usd": usage.get("cost_usd"),
    }


def turn_down_action(incident_id: int, detail: dict, truth: dict) -> dict:
    """Record the action Sherlog wants to run and reject it so the incident
    can close; the chaos reset is what repairs the system."""
    pending = next((a for a in detail.get("actions", []) if a["state"] == "awaiting_approval"), None)
    if not pending:
        return {}
    try:
        call("POST", f"{SHERLOG}/incidents/{incident_id}/reject", {"reason": "benchmark run: chaos reset repairs it"})
    except urllib.error.HTTPError as exc:
        log(f"incident #{incident_id}: could not reject the pending action ({exc.code})")
    return {
        "proposed_action": pending["type"],
        "proposed_target": pending["params"].get("service"),
        "action_acceptable": pending["type"] in truth.get("acceptable_actions", []),
    }


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
        "| Scenario | Truth | Detected | TTD (s) | Incidents | Hint | Diagnosis | LLM tokens | Log lines → tokens |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        diagnosis = (
            f"{r['diagnosis_service']} / {r['diagnosis_category']} {'✓' if r['cause_correct'] else '✗'}"
            if r.get("diagnosed") else "-"
        )
        rows.append(
            f"| {r['title']} | {r['truth_service']} / {r['truth_category']} | {'yes' if r['detected'] else 'no'} | "
            f"{r['ttd_s'] if r['ttd_s'] is not None else '-'} | {r.get('incidents', 0)} | "
            f"{r.get('hint_service') or '-'} {'✓' if r.get('hint_correct') else '✗'} | {diagnosis} | "
            f"{r.get('llm_tokens') or '-'} | {r.get('log_lines') or '-'} → {r.get('evidence_tokens') or '-'} |"
        )
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prefixes", nargs="*")
    parser.add_argument("--baseline-minutes", type=float, default=10)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--no-baseline", action="store_true")
    args = parser.parse_args()

    global SHERLOG_INVESTIGATES
    status = call("GET", f"{SHERLOG}/status")
    llm_mode, SHERLOG_INVESTIGATES = status.get("llm"), status.get("investigate", False)
    if llm_mode == "fake":
        log("Sherlog is running FAKE models: diagnoses below test the plumbing, not the reasoning")

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
        "median_ttd_s": median([r["ttd_s"] for r in results]),
        "baseline": baseline,
    }
    diagnosed = [r for r in results if r.get("diagnosed")]
    if diagnosed:
        costs = [r["llm_cost_usd"] for r in diagnosed]
        summary.update(
            {
                "llm_mode": llm_mode,
                "diagnosed": len(diagnosed),
                "service_correct": sum(r["service_correct"] for r in diagnosed),
                "category_correct": sum(r["category_correct"] for r in diagnosed),
                "cause_correct": sum(r["cause_correct"] for r in diagnosed),
                "median_time_to_diagnosis_s": median([r["time_to_diagnosis_s"] for r in diagnosed]),
                "median_llm_tokens": median([r["llm_tokens"] for r in diagnosed]),
                "total_llm_cost_usd": round(sum(costs), 4) if None not in costs else None,
            }
        )
    RESULTS.mkdir(exist_ok=True)
    kind = f"investigation-{llm_mode}" if diagnosed else "detection"
    out = RESULTS / f"{now():%Y-%m-%dT%H%M}-{kind}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print("\n" + table(results))
    print("\n" + json.dumps(summary, indent=2))
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
