"""Evidence funnel: compress an incident's telemetry into a small pack.

The LLM never sees raw logs. The pack holds, for the incident window and the
suspect services: the signals, change events (always), new or spiking log
templates with a few examples, exception fingerprints with a trimmed stack,
summaries of failing/slow traces with their error logs, and the graph-based
root-cause hint. We record how much raw data went in versus what came out.
"""

import json
import logging
from datetime import timedelta

from sherlog import db
from sherlog.config import SERVICES
from sherlog.detectors.logs import log_detector
from sherlog.funnel.heuristics import rank_root_cause
from sherlog.ingest.clients import loki, tempo
from sherlog.signals import Signal, utcnow
from sherlog.topology.graph import topology

log = logging.getLogger("sherlog.funnel")

LEAD_IN = timedelta(minutes=3)  # look a bit before the first anomaly
CHANGE_LOOKBACK = timedelta(minutes=15)  # deploys shortly before the incident are prime suspects
MAX_TEMPLATES = 12
MAX_EXCEPTIONS = 6
MAX_TRACES = 3


def _estimate_tokens(chars: int) -> int:
    return max(1, chars // 4)


def _attr(attributes: list[dict], key: str):
    for item in attributes or []:
        if item["key"] == key:
            return next(iter(item["value"].values()), None)
    return None


def summarize_trace(trace: dict, trace_id: str) -> dict:
    spans = []
    for resource_spans in trace.get("trace", {}).get("resourceSpans", []):
        service = _attr(resource_spans.get("resource", {}).get("attributes"), "service.name")
        for scope in resource_spans.get("scopeSpans", []):
            for span in scope.get("spans", []):
                start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
                status = span.get("status", {})
                spans.append(
                    {
                        "service": service,
                        "name": span["name"],
                        "kind": span.get("kind", "").removeprefix("SPAN_KIND_").lower(),
                        "start": start,
                        "duration_ms": round((end - start) / 1e6, 1),
                        "error": status.get("code") == "STATUS_CODE_ERROR",
                        "status_message": status.get("message"),
                        "statement": _attr(span.get("attributes"), "db.statement"),
                    }
                )
    if not spans:
        return {"trace_id": trace_id, "spans": []}
    spans.sort(key=lambda s: s["start"])
    root = spans[0]
    keep = [
        s for s in spans
        if s is root or s["error"] or s["duration_ms"] >= max(50.0, 0.2 * root["duration_ms"])
    ]
    root_start = root["start"]
    for s in keep:
        s["offset_ms"] = round((s.pop("start") - root_start) / 1e6, 1)
        if not s["statement"]:
            s.pop("statement")
        if not s["status_message"]:
            s.pop("status_message")
    return {
        "trace_id": trace_id,
        "root": f"{root['service']} {root['name']}",
        "duration_ms": root["duration_ms"],
        "span_count": len(spans),
        "error": any(s["error"] for s in spans),
        "critical_spans": keep[:12],
    }


async def _traces(services: set[str], start, end) -> list[dict]:
    names = "|".join(sorted(services & set(SERVICES))) or "gateway"
    query = f'{{ resource.service.name =~ "{names}" && (status = error || duration > 1s) }}'
    try:
        found = await tempo.search(query, start, end, limit=20)
    except Exception as exc:  # noqa: BLE001
        log.warning("Trace search failed: %s", exc)
        return []
    found.sort(key=lambda t: -int(t.get("durationMs", 0)))
    summaries = []
    for item in found[:MAX_TRACES]:
        try:
            summaries.append(summarize_trace(await tempo.trace(item["traceID"]), item["traceID"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("Trace fetch failed: %s", exc)
    return summaries


async def _trace_logs(trace_ids: list[str], start, end) -> list[dict]:
    rows = []
    for trace_id in trace_ids:
        try:
            streams = await loki.query_range(
                f'{{service_name=~".+"}} | trace_id="{trace_id}" | severity_text=~"WARN|ERROR"', start, end, limit=20
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Trace log query failed: %s", exc)
            continue
        lines = sorted(
            (ts, s["stream"].get("service_name"), line) for s in streams for ts, line in s["values"]
        )
        if lines:
            rows.append({"trace_id": trace_id, "lines": [f"[{svc}] {line[:240]}" for _, svc, line in lines[:8]]})
    return rows


async def _log_volume(services: set[str], seconds: int, end) -> int:
    regex = "|".join(sorted(services & set(SERVICES))) or ".+"
    try:
        result = await loki.query(f'sum(count_over_time({{service_name=~"{regex}"}}[{max(seconds, 60)}s]))', at=end)
    except Exception as exc:  # noqa: BLE001
        log.warning("Log volume query failed: %s", exc)
        return 0
    return int(float(result[0]["value"][1])) if result else 0


def _signal_summary(signal: Signal) -> dict:
    return {
        "type": signal.type,
        "service": signal.service,
        "severity": signal.severity,
        "started_at": signal.started_at.isoformat(),
        "active": signal.active,
        "details": signal.details,
    }


async def build_evidence(signals: list[Signal], services: list[str]) -> dict:
    first = min(s.started_at for s in signals)
    start, end = first - LEAD_IN, utcnow()

    suspects = set(services)
    for service in list(suspects):
        suspects |= topology.downstream(service)
    shop_suspects = suspects & set(SERVICES)

    changes = await db.events_between(first - CHANGE_LOOKBACK, end)
    templates = log_detector.interesting_templates(shop_suspects)[:MAX_TEMPLATES]
    exceptions = log_detector.interesting_exceptions(shop_suspects)[:MAX_EXCEPTIONS]
    traces = await _traces(shop_suspects, start, end)
    trace_logs = await _trace_logs([t["trace_id"] for t in traces], start, end)
    hint = rank_root_cause(signals)

    pack = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "suspect_services": sorted(suspects),
        "root_cause_hint": hint,
        "signals": [_signal_summary(s) for s in sorted(signals, key=lambda s: s.started_at)],
        "changes": changes,
        "log_templates": templates,
        "exceptions": exceptions,
        "traces": traces,
        "trace_logs": trace_logs,
        "topology": topology.to_dict()["edges"],
    }
    chars = len(json.dumps(pack, default=str))
    # Every shop log line in the window went through the detectors and the funnel.
    lines = await _log_volume(set(SERVICES), int((end - start).total_seconds()), end)
    pack["stats"] = {
        "log_lines_in_window": lines,
        "templates_known": sum(1 for t in log_detector.templates.values() if t.service in shop_suspects),
        "templates_selected": len(templates),
        "exceptions_selected": len(exceptions),
        "traces_selected": len(traces),
        "evidence_chars": chars,
        "estimated_tokens": _estimate_tokens(chars),
    }
    return pack
