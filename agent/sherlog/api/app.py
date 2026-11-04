"""Sherlog API: incidents, actions, postmortems, signals, topology, change
events, settings and a live stream. Every route except /health and
/auth/login needs an API key or a session (see sherlog.api.auth)."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from sherlog import db, llm
from sherlog.actions.policy import policy
from sherlog.api.auth import (
    COOKIE, SAFE_METHODS, Principal, admin, auth, check, client_ip, operator, viewer,
)
from sherlog.api.stream import broadcaster
from sherlog.config import settings
from sherlog.detectors.containers import container_detector
from sherlog.detectors.logs import log_detector
from sherlog.detectors.metrics import metric_detector
from sherlog.incidents.manager import ActionError, manager
from sherlog.investigator import tools
from sherlog.ingest.clients import platform
from sherlog.llm.guard import breaker, budget
from sherlog.signals import bus, utcnow
from sherlog.topology.graph import topology

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sherlog")

EVENT_POLL_S = 5


async def backfill_events() -> None:
    """Pick up change events emitted while Sherlog was down."""
    try:
        events = await platform.events(limit=500)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not backfill change events: %s", exc)
        return
    added = sum([await db.insert_event(e) for e in events])
    log.info("Backfilled %d change events", added)


async def poll_events() -> None:
    """Pull recent change events every few seconds as well, so none is lost
    when a push from the platform fails or isn't authenticated."""
    while True:
        await asyncio.sleep(EVENT_POLL_S)
        try:
            events = await platform.events(limit=50)
        except Exception as exc:  # noqa: BLE001
            log.debug("Change event poll failed: %s", exc)
            continue
        for event in events:
            if await db.insert_event(event):
                broadcaster.publish("change", {"event": event})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await auth.load()
    await policy.load()
    await manager.load()
    await backfill_events()
    tasks = [
        asyncio.create_task(coro, name=name)
        for name, coro in (
            ("topology", topology.run()),
            ("metrics", metric_detector.run()),
            ("logs", log_detector.run()),
            ("containers", container_detector.run()),
            ("incidents", manager.run()),
            ("watchdog", manager.watchdog()),
            ("events", poll_events()),
        )
    ]
    for task in tasks:
        task.add_done_callback(_crash_logger)
    yield
    for task in tasks:
        task.cancel()
    if not log_detector.learning:
        await log_detector.save()


def _crash_logger(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("Background task %s crashed", task.get_name(), exc_info=task.exception())


app = FastAPI(title="Sherlog", version="0.4.0", lifespan=lifespan)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )


@app.middleware("http")
async def audit(request: Request, call_next):
    """Record every write and every refused request: who, what, when, result."""
    response = await call_next(request)
    if request.method not in SAFE_METHODS or response.status_code in (401, 403, 429):
        if request.url.path == "/auth/check" and response.status_code < 400:
            return response
        principal: Principal | None = getattr(request.state, "principal", None)
        path = request.headers.get("x-original-uri", request.url.path) if request.url.path == "/auth/check" else request.url.path
        try:
            await db.insert_audit(
                principal.name if principal else None, principal.role if principal else None,
                request.headers.get("x-original-method", request.method), path, response.status_code, client_ip(request),
            )
        except Exception as exc:  # noqa: BLE001 - auditing must not take requests down
            log.warning("Audit write failed: %s", exc)
    return response


# ------------------------------------------------------------ health and access
@app.get("/health")
async def health():
    """Liveness for probes: no details, no authentication."""
    return {"status": "ok", "learning": log_detector.learning}


@app.get("/status")
async def status(principal: Principal = viewer):
    return {
        "status": "ok",
        "learning": log_detector.learning,
        "llm": llm.mode(),
        "investigate": settings.investigate,
        **policy.to_dict(),
        "user": principal.to_dict(),
        "logs": log_detector.stats(),
        "models": {"roles": llm.describe(), "breaker": breaker.state(), "budget": await budget.state(),
                   "max_concurrent_investigations": settings.max_concurrent_investigations},
    }


class Login(BaseModel):
    token: str


@app.post("/auth/login")
async def login(body: Login, request: Request, response: Response):
    if not auth.allow_login(client_ip(request) or "?"):
        raise HTTPException(status_code=429, detail="too many attempts; wait a minute")
    principal = auth.principal_for(body.token.strip())
    if principal is None:
        raise HTTPException(status_code=401, detail="unknown API key")
    request.state.principal = principal
    response.set_cookie(
        COOKIE, auth.session_cookie(principal), max_age=settings.session_hours * 3600,
        httponly=True, samesite="strict", secure=settings.secure_cookies, path="/",
    )
    return principal.to_dict()


@app.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/auth/me")
async def me(principal: Principal = viewer):
    return principal.to_dict()


@app.get("/auth/check", status_code=204)
async def auth_check(request: Request):
    """For nginx auth_request: reads go to viewers, writes to operators."""
    method = request.headers.get("x-original-method", "GET")
    check(request, "viewer" if method in SAFE_METHODS else "operator", method)
    return Response(status_code=204)


class KeyRequest(BaseModel):
    name: str
    role: str


@app.get("/auth/keys")
async def list_keys(principal: Principal = admin):
    return auth.list_keys()


@app.post("/auth/keys", status_code=201)
async def create_key(body: KeyRequest, principal: Principal = admin):
    try:
        token = await auth.create_key(body.name.strip(), body.role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"name": body.name, "role": body.role, "token": token}


@app.delete("/auth/keys/{name}")
async def revoke_key(name: str, principal: Principal = admin):
    if name == principal.name:
        raise HTTPException(status_code=409, detail="you can't revoke the key you are using")
    if not await auth.revoke(name):
        raise HTTPException(status_code=404, detail="no key with that name was created through the API")
    return {"revoked": name}


@app.get("/audit")
async def audit_log(limit: int = 200, principal: Principal = admin):
    return await db.recent_audit(min(limit, 1000))


# ------------------------------------------------------------ incidents
@app.get("/incidents")
async def list_incidents(status: str = "all", principal: Principal = viewer):
    incidents = sorted(manager.incidents.values(), key=lambda i: i.id, reverse=True)
    if status == "open":
        incidents = [i for i in incidents if i.open]
    if incidents or status == "open":
        return [i.summary() for i in incidents]
    return [
        {k: row[k] for k in ("id", "status", "title", "services", "created_at", "updated_at", "resolved_at")}
        for row in await db.recent_incidents()
    ]


def _incident(incident_id: int):
    incident = manager.incidents.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="unknown incident (or from a previous run)")
    return incident


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: int, principal: Principal = viewer):
    return _incident(incident_id).to_dict()


@app.post("/incidents/{incident_id}/evidence")
async def rebuild_evidence(incident_id: int, principal: Principal = operator):
    return await manager.refresh_evidence(_incident(incident_id))


@app.post("/incidents/{incident_id}/investigate", status_code=202)
async def start_investigation(incident_id: int, principal: Principal = operator):
    """Run (or re-run) triage and the investigator on a live incident."""
    incident = _incident(incident_id)
    if not incident.live:
        raise HTTPException(status_code=409, detail=f"incident is {incident.status}")
    if incident.busy:
        raise HTTPException(status_code=409, detail="already investigating or acting")
    manager.spawn(manager.investigate(incident))
    return {"accepted": True}


class Rejection(BaseModel):
    reason: str | None = None


class ActionRequest(BaseModel):
    type: str
    params: dict = {}


# The actor is always the authenticated caller; any "actor" in a body is ignored.
@app.post("/incidents/{incident_id}/approve")
async def approve_action(incident_id: int, principal: Principal = operator):
    """Approve the action Sherlog is waiting on (autonomy L1, or L2 above low risk)."""
    try:
        return await manager.approve(_incident(incident_id), principal.name)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/incidents/{incident_id}/reject")
async def reject_action(incident_id: int, body: Rejection | None = None, principal: Principal = operator):
    """Turn down the pending action; the incident is escalated."""
    try:
        return await manager.reject(_incident(incident_id), principal.name, (body or Rejection()).reason)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/incidents/{incident_id}/actions", status_code=202)
async def run_action(incident_id: int, body: ActionRequest, principal: Principal = operator):
    """Have Sherlog run an action a human chose, with the same verification and revert."""
    try:
        return await manager.run_action(_incident(incident_id), body.type, body.params, principal.name)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/incidents/{incident_id}/postmortem", response_class=PlainTextResponse)
async def get_postmortem(incident_id: int, principal: Principal = viewer):
    incident = manager.incidents.get(incident_id)
    text = incident.postmortem if incident else ((await db.get_incident(incident_id)) or {}).get("data", {}).get("postmortem")
    if not text:
        raise HTTPException(status_code=404, detail="no postmortem for this incident")
    return PlainTextResponse(text, media_type="text/markdown")


@app.get("/postmortems")
async def list_postmortems(limit: int = 50, principal: Principal = viewer):
    return await db.postmortems(limit)


# ------------------------------------------------------------ settings
@app.get("/settings")
async def get_settings(principal: Principal = viewer):
    return policy.to_dict()


class SettingsUpdate(BaseModel):
    autonomy: str | None = None
    dry_run: bool | None = None


@app.put("/settings")
async def update_settings(body: SettingsUpdate, principal: Principal = admin):
    """Autonomy level (L0 suggest, L1 approve, L2 auto for low risk) and dry run."""
    try:
        updated = await policy.update(body.autonomy, body.dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    broadcaster.publish("settings", {"settings": updated, "by": principal.name})
    return updated


# ------------------------------------------------------------ tools and detectors
@app.get("/tools")
async def list_tools(principal: Principal = viewer):
    return tools.specs()


@app.post("/tools/{name}")
async def call_tool(name: str, arguments: dict, incident_id: int | None = None, principal: Principal = viewer):
    """Run an investigator tool by hand (debugging, and the UI's evidence panel).
    Tools are read-only. Time ranges default to the incident's window, or the
    last 15 minutes."""
    incident = manager.incidents.get(incident_id) if incident_id else None
    start = (
        datetime.fromisoformat(incident.evidence["window"]["start"])
        if incident and incident.evidence
        else utcnow() - timedelta(minutes=15)
    )
    return await tools.run_tool(tools.ToolContext(incident_id or 0, start), name, arguments)


@app.get("/signals")
async def active_signals(principal: Principal = viewer):
    return [s.to_dict() for s in bus.active.values()]


@app.get("/topology")
async def get_topology(principal: Principal = viewer):
    data = topology.to_dict()
    unhealthy = {s.service for s in bus.active.values()}
    for node in data["nodes"]:
        node["status"] = "anomalous" if node["id"] in unhealthy else "ok"
    return data


@app.get("/detectors/metrics")
async def metric_state(principal: Principal = viewer):
    return metric_detector.snapshot()


@app.get("/detectors/logs")
async def log_state(principal: Principal = viewer):
    return {
        **log_detector.stats(),
        "interesting_templates": log_detector.interesting_templates()[:20],
        "interesting_exceptions": log_detector.interesting_exceptions()[:10],
    }


# ------------------------------------------------------------ change events
@app.post("/events", status_code=202)
async def receive_event(event: dict, request: Request):
    """Change events pushed by the deploy platform (deploys, config changes,
    flags), authenticated with SHERLOG_EVENTS_TOKEN or an operator key."""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not (settings.events_token and token and hmac.compare_digest(token, settings.events_token)):
        check(request, "operator")
    if not {"id", "type", "timestamp"} <= event.keys():
        raise HTTPException(status_code=422, detail="event needs id, type and timestamp")
    if await db.insert_event(event):
        broadcaster.publish("change", {"event": event})
    return {"accepted": True}


@app.get("/events")
async def list_events(limit: int = 100, principal: Principal = viewer):
    return await db.recent_events(limit)


@app.get("/stats")
async def stats(principal: Principal = viewer):
    incidents = list(manager.incidents.values())
    evidence = [i.evidence["stats"] for i in incidents if i.evidence]
    return {
        "incidents_total": len(incidents),
        "incidents_open": sum(1 for i in incidents if i.open),
        "log_lines_seen": log_detector.lines_seen,
        "log_lines_parsed": log_detector.lines_parsed,
        "log_templates": len(log_detector.templates),
        "exception_fingerprints": len(log_detector.exceptions),
        "funnel": {
            "log_lines_in_windows": sum(s["log_lines_in_window"] for s in evidence),
            "estimated_tokens": sum(s["estimated_tokens"] for s in evidence),
        },
        "llm": {"mode": llm.mode(), "by_model": await db.llm_totals()},
    }


@app.get("/stream")
async def stream(principal: Principal = viewer):
    return StreamingResponse(
        broadcaster.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
