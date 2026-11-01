"""Tools the investigator can call.

All of them are read-only windows onto what an on-call engineer can see:
telemetry, the deploy platform's read API, the deployed source code and
Sherlog's own records. Each returns a compact summary, never raw series or
unbounded logs, and errors come back as {"error": ...} for the model to read.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from sherlog import db
from sherlog.config import SERVICES, settings
from sherlog.detectors.logs import log_detector, trim_stack
from sherlog.detectors.metrics import metric_detector
from sherlog.funnel.builder import summarize_trace
from sherlog.ingest.clients import loki, platform, prometheus, tempo
from sherlog.memory import store as memory
from sherlog.signals import bus, utcnow
from sherlog.topology.graph import topology

MAX_RESULT_CHARS = 6000
MAX_DIFF_CHARS = 4000
DEMO_ROOT = "demo-app"
DATASTORES = ("postgresql", "redis")


@dataclass
class ToolContext:
    incident_id: int
    start: datetime  # default start for time ranges: the incident's evidence window


TOOLS: dict[str, dict] = {}

SHOP_SERVICE = {"type": "string", "enum": list(SERVICES)}
ANY_SERVICE = {"type": "string", "enum": [*SERVICES, *DATASTORES]}
TIME = {"type": "string", "description": "ISO 8601 timestamp, or relative like -15m / -2h. Defaults to the incident window."}


def tool(description: str, **params: tuple[dict, bool]):
    """Register a tool. Each param is (JSON schema, required)."""

    def register(fn):
        TOOLS[fn.__name__] = {
            "fn": fn,
            "params": params,
            "spec": {
                "name": fn.__name__,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {name: schema for name, (schema, _) in params.items()},
                    "required": [name for name, (_, required) in params.items() if required],
                    "additionalProperties": False,
                },
            },
        }
        return fn

    return register


def specs() -> list[dict]:
    return [entry["spec"] for entry in TOOLS.values()]


async def run_tool(ctx: ToolContext, name: str, arguments: dict):
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool {name}; available: {', '.join(TOOLS)}"}
    if "_malformed" in arguments:
        return {"error": "arguments were not valid JSON"}
    unknown = set(arguments) - set(entry["params"])
    missing = {p for p, (_, required) in entry["params"].items() if required} - set(arguments)
    if unknown or missing:
        return {"error": f"bad arguments: unknown {sorted(unknown)}, missing {sorted(missing)}"}
    for param, value in arguments.items():
        allowed = entry["params"][param][0].get("enum")
        if allowed and value not in allowed:
            return {"error": f"bad arguments: {param} must be one of {allowed}"}
    try:
        return await asyncio.wait_for(entry["fn"](ctx, **arguments), settings.tool_timeout_s)
    except asyncio.TimeoutError:
        return {"error": f"timed out after {settings.tool_timeout_s:.0f}s"}
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:  # noqa: BLE001 - the model should see the failure, not crash the loop
        return {"error": f"{type(exc).__name__}: {exc}"}


def render(result) -> str:
    text = json.dumps(result, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " ... [truncated]"


# ---------------------------------------------------------------- helpers
RELATIVE = re.compile(r"^-(\d+)([smh])$")
UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


def _time(value: str | None, default: datetime) -> datetime:
    if not value or value == "now":
        return default
    if match := RELATIVE.match(value.strip()):
        return utcnow() - timedelta(**{UNITS[match[2]]: int(match[1])})
    ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _r(value: float | None) -> float | None:
    if value is None:
        return None
    return float(f"{value:.4g}")


async def _deployed_version(service: str) -> str:
    entry = next((s for s in await platform.services() if s["service"] == service), None)
    if entry is None or not entry.get("version"):
        raise ValueError(f"{service} is not a deployable service")
    return entry["version"]


# ---------------------------------------------------------------- tools
@tool(
    "Run a PromQL range query and get per-series min/max/mean/last plus about 12 sampled points. "
    "Shop metrics: http_server_duration_seconds_{count,bucket} (labels job=service, route, status_code), "
    "http_client_duration_seconds_{count,bucket} (job, peer, outcome), process_cpu_time_seconds_total, "
    "process_memory_rss_bytes, process_memory_limit_bytes, db_pool_connections_in_use, db_pool_connections_max, "
    "traces_service_graph_request_total and traces_service_graph_request_failed_total (client, server).",
    promql=({"type": "string"}, True),
    start=(TIME, False),
    end=(TIME, False),
)
async def query_metrics(ctx: ToolContext, promql: str, start: str | None = None, end: str | None = None):
    t0, t1 = _time(start, ctx.start), _time(end, utcnow())
    step = max(15.0, (t1 - t0).total_seconds() / 24)
    result = await prometheus.query_range(promql, t0, t1, step)
    series = []
    for row in result[:10]:
        values = [float(v) for _, v in row["values"] if v not in ("NaN", "+Inf", "-Inf")]
        if not values:
            continue
        stride = max(1, len(values) // 12)
        series.append(
            {
                "labels": row["metric"],
                "min": _r(min(values)),
                "max": _r(max(values)),
                "mean": _r(sum(values) / len(values)),
                "first": _r(values[0]),
                "last": _r(values[-1]),
                "points": [_r(v) for v in values[::stride]][:12],
            }
        )
    return {"start": t0.isoformat(), "end": t1.isoformat(), "step_s": step, "series_total": len(result), "series": series}


@tool(
    "Current health of a service or datastore: deployed version, containers (status, health, restarts, "
    "OOM kills, exit codes), each watched metric's current value against its learned baseline, and active signals.",
    service=(ANY_SERVICE, True),
)
async def get_service_health(ctx: ToolContext, service: str):
    entry = next((s for s in await platform.services() if s["service"] == service), None)
    health = {"service": service}
    if entry:
        health.update({k: entry[k] for k in ("version", "deployed_at", "replicas") if k in entry})
        health["containers"] = [
            {k: c.get(k) for k in ("status", "health", "restart_count", "oom_killed", "exit_code", "started_at")}
            for c in entry.get("containers", [])
        ]
    health["metrics"] = [
        {
            "metric": row["rule"] + (f"->{row['qualifier']}" if row["qualifier"] else ""),
            "value": _r(row["value"]),
            "baseline": _r(row["baseline"]),
            "anomalous": row["firing"],
        }
        for row in metric_detector.snapshot()
        if row["service"] == service
    ]
    health["active_signals"] = [
        {"type": s.type, "since": s.started_at.isoformat(), "details": s.details} for s in bus.active_for(service)
    ]
    return health


@tool(
    "Find traces through a service and get their critical path (slow or failing spans, DB statements, error messages).",
    service=(SHOP_SERVICE, True),
    status=({"type": "string", "enum": ["error", "slow", "any"]}, False),
    min_duration_ms=({"type": "integer", "description": "For status=slow; default 1000."}, False),
    start=(TIME, False),
    end=(TIME, False),
    limit=({"type": "integer", "minimum": 1, "maximum": 5}, False),
)
async def get_traces(
    ctx: ToolContext, service: str, status: str = "error", min_duration_ms: int = 1000,
    start: str | None = None, end: str | None = None, limit: int = 3,
):
    condition = {
        "error": " && status = error",
        "slow": f" && duration > {int(min_duration_ms)}ms",
        "any": "",
    }.get(status, " && status = error")
    t0, t1 = _time(start, ctx.start), _time(end, utcnow())
    found = await tempo.search(f'{{ resource.service.name = "{service}"{condition} }}', t0, t1, limit=20)
    found.sort(key=lambda t: -int(t.get("durationMs", 0)))
    traces = [summarize_trace(await tempo.trace(t["traceID"]), t["traceID"]) for t in found[: min(limit, 5)]]
    return {"matched": len(found), "traces": traces}


@tool(
    "Read recent log lines of a service, newest first, filtered by a Drain template id (from the evidence), "
    "a literal substring, and/or a level. At most 20 lines.",
    service=(SHOP_SERVICE, True),
    template_id=({"type": "string"}, False),
    query=({"type": "string", "description": "Literal substring to match."}, False),
    level=({"type": "string", "enum": ["INFO", "WARN", "ERROR"]}, False),
    start=(TIME, False),
    end=(TIME, False),
    limit=({"type": "integer", "minimum": 1, "maximum": 20}, False),
)
async def get_logs(
    ctx: ToolContext, service: str, template_id: str | None = None, query: str | None = None,
    level: str | None = None, start: str | None = None, end: str | None = None, limit: int = 20,
):
    result: dict = {}
    needle = query
    if template_id:
        template = log_detector.templates.get(template_id)
        if template is None:
            return {"error": f"unknown template_id {template_id}"}
        result["template"] = {
            "id": template.id,
            "text": template.text,
            "first_seen": template.first_seen.isoformat(),
            "seen_before_incident": template.learned,
            "per_minute": template.counter.per_minute,
            "baseline_per_minute": _r(template.counter.baseline.mean),
            "last_10_minutes": [count for _, count in list(template.history)[-10:]],
        }
        # Loki can't match a Drain template, so filter on its longest literal stretch.
        needle = needle or max(re.split(r"<[^>]+>", template.text), key=len).strip()
    logql = f'{{service_name="{service}"}}'
    if needle:
        logql += f" |= {json.dumps(needle)}"
    if level:
        logql += f' | severity_text=~"{level}|{level}ING"' if level == "WARN" else f' | severity_text="{level}"'
    t0, t1 = _time(start, ctx.start), _time(end, utcnow())
    streams = await loki.query_range(logql, t0, t1, limit=min(limit, 20), direction="backward")
    lines = sorted(
        ((ts, s["stream"].get("severity_text", ""), line) for s in streams for ts, line in s["values"]), reverse=True
    )
    result["logql"] = logql
    result["lines"] = [
        f"{datetime.fromtimestamp(int(ts) / 1e9, timezone.utc):%H:%M:%S} {lvl} {line[:300]}"
        for ts, lvl, line in lines[: min(limit, 20)]
    ]
    return result


@tool(
    "Details of an exception fingerprint from the evidence: type, message, own-code frames, trimmed stack trace, "
    "counts, the service version that raised it and sample trace ids.",
    fingerprint=({"type": "string"}, True),
)
async def get_exception(ctx: ToolContext, fingerprint: str):
    exc = log_detector.exceptions.get(fingerprint)
    if exc is None:
        return {"error": f"unknown fingerprint {fingerprint}"}
    return {
        "fingerprint": exc.fingerprint,
        "service": exc.service,
        "type": exc.type,
        "message": exc.message,
        "version": exc.version,
        "first_seen": exc.first_seen.isoformat(),
        "seen_before_incident": exc.learned,
        "per_minute": exc.counter.per_minute,
        "total": exc.counter.total,
        "frames": exc.frames,
        "stack": trim_stack(exc.stack),
        "trace_ids": list(exc.trace_ids),
    }


@tool(
    "Read source code of the demo app around a line or a function, at the version a service runs "
    "(or another version). Paths look like 'orders/pricing.py' or 'common/http.py'.",
    service=(SHOP_SERVICE, True),
    file=({"type": "string"}, True),
    line=({"type": "integer", "minimum": 1}, False),
    function=({"type": "string", "description": "Function name to centre on, instead of a line."}, False),
    radius=({"type": "integer", "minimum": 3, "maximum": 40}, False),
    version=({"type": "string", "description": "Defaults to the deployed version."}, False),
)
async def get_code_context(
    ctx: ToolContext, service: str, file: str, line: int | None = None, function: str | None = None,
    radius: int = 15, version: str | None = None,
):
    version = version or await _deployed_version(service)
    path = file.strip().lstrip("/").removeprefix("app/")
    if not path.startswith(f"{DEMO_ROOT}/"):
        path = f"{DEMO_ROOT}/{path}"
    lines = (await platform.source(service, version, path)).splitlines()
    if function and not line:
        line = next((n for n, text in enumerate(lines, 1) if re.match(rf"\s*(async\s+)?def {re.escape(function)}\b", text)), None)
        if line is None:
            return {"error": f"no function {function} in {path} at {version}"}
    centre = line or 1
    lo, hi = max(1, centre - radius), min(len(lines), centre + radius)
    return {
        "service": service,
        "version": version,
        "file": path,
        "lines": "\n".join(f"{n:4d}{'>' if n == line else ' '} {lines[n - 1]}" for n in range(lo, hi + 1)),
    }


@tool(
    "Change events (deploys with commit message and changed files, config changes, feature flags, restarts, "
    "scaling) for a service, plus global flag changes, newest last.",
    service=(ANY_SERVICE, False),
    since=(TIME, False),
)
async def get_recent_changes(ctx: ToolContext, service: str | None = None, since: str | None = None):
    events = await db.events_between(_time(since, ctx.start - timedelta(minutes=30)), utcnow())
    if service:
        events = [e for e in events if e.get("service") in (service, None)]
    keep = ("id", "timestamp", "type", "service", "actor", "from_version", "to_version", "commit_message", "changed_files", "details")
    return [{k: e[k] for k in keep if e.get(k) not in (None, [], {})} for e in events[-20:]]


@tool(
    "Summarised diff of a service's code between two deployed versions (as shown in change events).",
    service=(SHOP_SERVICE, True),
    from_version=({"type": "string"}, True),
    to_version=({"type": "string"}, True),
)
async def diff_deploy(ctx: ToolContext, service: str, from_version: str, to_version: str):
    diff = await platform.diff(service, from_version, to_version)
    files: dict[str, dict] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1]
            files[current] = {"file": current, "added": 0, "removed": 0}
        elif current and line.startswith("+") and not line.startswith("+++"):
            files[current]["added"] += 1
        elif current and line.startswith("-") and not line.startswith("---"):
            files[current]["removed"] += 1
    return {
        "files": list(files.values()),
        "diff": diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]",
    }


@tool(
    "A service's neighbours in the live dependency graph (from traces): what it calls and what calls it, "
    "with request rate, error rate and whether each neighbour is currently anomalous.",
    service=(ANY_SERVICE, True),
)
async def get_dependencies(ctx: ToolContext, service: str):
    def edge(other: str, stats: dict) -> dict:
        return {
            "service": other,
            "rps": stats.get("rps"),
            "error_rate": stats.get("error_rate"),
            "anomalous": bool(bus.active_for(other)),
        }

    return {
        "service": service,
        "calls": [edge(server, s) for (client, server), s in topology.edges.items() if client == service],
        "called_by": [edge(client, s) for (client, server), s in topology.edges.items() if server == service],
    }


@tool(
    "Closed incidents (incident memory) that share an exception fingerprint or a log template, or whose "
    "root cause was a service: their root cause, outcome and the action that fixed them.",
    fingerprint=({"type": "string"}, False),
    template=({"type": "string", "description": "Template text or template id."}, False),
    service=(ANY_SERVICE, False),
)
async def search_past_incidents(
    ctx: ToolContext, fingerprint: str | None = None, template: str | None = None, service: str | None = None
):
    if not (fingerprint or template or service):
        return {"error": "give a fingerprint, a template or a service"}
    return await memory.search(ctx.incident_id, fingerprint, template, service)
