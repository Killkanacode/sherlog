"""Postmortem in Markdown, written when an incident closes.

Built from what Sherlog recorded (signals, timeline, diagnosis, actions and
their verification) plus the impact measured at the gateway, so it needs no
model call and is ready the moment the incident closes.
"""

import logging
from datetime import datetime

from sherlog.actions.execute import describe as describe_action
from sherlog.actions.verify import describe as describe_problems
from sherlog.ingest.clients import prometheus
from sherlog.memory.store import describe as describe_match
from sherlog.signals import describe as describe_signal

log = logging.getLogger("sherlog.postmortem")

TIMELINE_KINDS = ("status", "evidence", "seen_before", "triage", "diagnosis", "action", "investigation_failed")
FOLLOW_UPS = {
    "bad_deploy_code_bug": [
        "Fix the bug with a regression test before redeploying the change",
        "Gate deploys on error rate (canary) so a bad build rolls back on its own",
    ],
    "config_change": ["Validate this setting before it is applied", "Roll out config and flag changes progressively"],
    "memory_exhaustion": ["Find and fix the leak", "Alert on memory growth well before the container limit"],
    "cpu_exhaustion": ["Profile the hot path and fix the algorithm", "Load-test checkout before deploys"],
    "db_slow_query": ["Restore the index or fix the query plan", "Alert on slow queries per endpoint"],
    "db_pool_exhaustion": ["Size the connection pool for peak load", "Alert on pool saturation"],
    "network_latency": ["Tighten client timeouts and add a circuit breaker on the slow dependency"],
    "network_partition": ["Add a fallback for the unreachable dependency", "Retry with backoff, within a budget"],
    "dependency_down": ["Make the datastore highly available, or degrade gracefully without it"],
    "process_crash": ["Find why the process died (exit code, last logs)", "Check the restart policy"],
    "process_hang": ["Add a liveness check that restarts a hung process"],
    "retry_storm": ["Retry with exponential backoff and jitter", "Cap retries with a retry budget"],
}


async def gateway_impact(start: datetime, end: datetime) -> dict | None:
    """Requests that reached the shop, and how many failed, between start and end."""
    seconds = max(60, round((end - start).total_seconds()))
    query = 'sum(increase(http_server_duration_seconds_count{job="gateway"%s}[%ds]))'
    try:
        total = await prometheus.query(query % ("", seconds), at=end)
        failed = await prometheus.query(query % (',status_code=~"5.."', seconds), at=end)
    except Exception as exc:  # noqa: BLE001 - impact is best effort
        log.warning("Impact query failed: %s", exc)
        return None
    value = lambda result: round(float(result[0]["value"][1])) if result else 0  # noqa: E731
    return {"requests": value(total), "failed": value(failed)}


def write(incident, impact_start: datetime, recovered_at: datetime, impact: dict | None) -> str:
    cause = (incident.diagnosis or {}).get("root_cause")
    known = bool(cause and cause["confidence"] > 0)
    fixed = next((a for a in incident.actions if a["state"] == "recovered"), None)
    executed = [a for a in incident.actions if a.get("executed_at")]
    first_action = min((datetime.fromisoformat(a["executed_at"]) for a in executed), default=None)

    if fixed:
        outcome = f"Resolved by Sherlog: {describe_action(fixed['type'], fixed['params'])}, recovery verified"
    elif incident.status == "escalated":
        outcome = "Escalated to a human; service recovered afterwards"
    else:
        outcome = "Recovered without action from Sherlog"

    rows = [
        ("Outcome", outcome),
        ("Impact", f"{_duration(recovered_at - impact_start)} ({_clock(impact_start)} to {_clock(recovered_at)} UTC)"),
    ]
    if impact and impact["requests"]:
        share = impact["failed"] / impact["requests"]
        rows.append(("Requests affected", f"{impact['failed']:,} of {impact['requests']:,} shop requests failed ({share:.1%})"))
    rows.append(("Services", ", ".join(incident.services)))
    if known:
        rows.append(("Root cause", f"{cause['service']} · {cause['category']} · confidence {cause['confidence']:.2f}"))
    rows.append(
        (
            "Detect / mitigate / recover",
            " / ".join(
                [
                    _duration(incident.created_at - impact_start),
                    _duration(first_action - incident.created_at) if first_action else "-",
                    _duration(recovered_at - incident.created_at),
                ]
            )
            + " (from the first anomaly, then from detection)",
        )
    )
    if incident.seen_before:
        rows.append(("Seen before", describe_match(incident.seen_before[0])))
    totals = incident.usage.totals() if incident.usage.calls else None
    if totals:
        cost = f"${totals['cost_usd']:.4f}" if totals["cost_usd"] is not None else "cost unknown"
        rows.append(("Model usage", f"{totals['calls']} calls, {totals['total_tokens']:,} tokens, {cost}"))

    out = [f"# Postmortem: incident #{incident.id}, {incident.title}", "", "| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]

    out += ["", "## Summary", "", _summary(incident, cause if known else None, fixed, impact_start, recovered_at)]

    out += ["", "## Root cause", ""]
    if known:
        out.append(cause["summary"])
        evidence = incident.diagnosis.get("evidence") or []
        if evidence:
            out += ["", "Evidence:", ""]
            out += [f"- **{e['type']}** `{e['ref']}`: {e['detail']}" for e in evidence]
    else:
        hint = (incident.evidence or {}).get("root_cause_hint") or {}
        out.append(
            "No confident diagnosis was reached."
            + (f" The graph heuristic pointed at **{hint['service']}** ({hint.get('reason')})." if hint.get("service") else "")
        )

    ruled_out = [f"- {r['hypothesis']}: {r['reason']}" for r in ((incident.diagnosis or {}).get("ruled_out") or [])]
    for action in executed:
        if action["state"] in ("reverted", "not_recovered"):
            remaining = describe_problems((action.get("verification") or {}).get("remaining") or [])
            ruled_out.append(
                f"- Mitigation \"{describe_action(action['type'], action['params'])}\" did not restore service"
                + (f" ({remaining})" if remaining else "")
                + ("; it was reverted" if action["state"] == "reverted" else "")
            )
    if ruled_out:
        out += ["", "## Ruled out", "", *ruled_out]

    out += ["", "## Timeline (UTC)", "", "| Time | Event |", "|---|---|"]
    out.append(f"| {_clock(impact_start)} | First anomaly: {_first_signal(incident)} |")
    tools = 0
    for entry in incident.timeline:
        if entry["kind"] == "tool":
            tools += 1
        if entry["kind"] in TIMELINE_KINDS:
            text = entry["text"].replace("|", "\\|").replace("\n", " ")
            out.append(f"| {_clock(datetime.fromisoformat(entry['ts']))} | {text[:300]} |")
    if tools:
        out += ["", f"The investigator made {tools} tool calls; the full trail is in the incident timeline."]

    follow_ups = list(FOLLOW_UPS.get(cause["category"], []) if known else [])
    if incident.seen_before:
        follow_ups.insert(0, f"Prioritize the permanent fix: this happened before, in {describe_match(incident.seen_before[0])}")
    if incident.status == "escalated":
        follow_ups.append("Review why Sherlog could not mitigate this on its own")
    if not known:
        follow_ups.append("Confirm the root cause by hand and record it")
    out += ["", "## Follow-up actions", "", *[f"- [ ] {item}" for item in follow_ups]]
    return "\n".join(out) + "\n"


def _summary(incident, cause: dict | None, fixed: dict | None, impact_start: datetime, recovered_at: datetime) -> str:
    parts = [
        f"Sherlog detected the incident {_duration(incident.created_at - impact_start)} after the first anomaly"
        f" ({_first_signal(incident)})."
    ]
    if cause:
        parts.append(f"The investigator traced it to **{cause['service']}** ({cause['category']}): {cause['summary']}")
    if fixed:
        verification = fixed.get("verification") or {}
        by = fixed.get("proposed_by")
        who = f" (requested by {by})" if by != "sherlog" else (
            f" (approved by {fixed['approved_by']})" if fixed.get("approved_by") not in (None, "sherlog") else ""
        )
        parts.append(
            f"Sherlog ran \"{describe_action(fixed['type'], fixed['params'])}\"" + who
            + f" and verified that the services were back to baseline {verification.get('seconds', '?')} s later."
        )
    elif incident.status == "escalated":
        parts.append("Sherlog escalated to a human instead of acting.")
    parts.append(f"Service was back to normal after {_duration(recovered_at - impact_start)} in total.")
    return " ".join(parts)


def _first_signal(incident) -> str:
    first = min(incident.signals, key=lambda s: s.started_at)
    return f"{first.service}: {describe_signal(first)}"


def _clock(ts: datetime) -> str:
    return ts.strftime("%H:%M:%S")


def _duration(delta) -> str:
    seconds = max(0, round(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"
