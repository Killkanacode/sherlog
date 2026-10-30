"""Sherlog API: incidents, signals, topology, change events and a live stream."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from sherlog import db, llm
from sherlog.api.stream import broadcaster
from sherlog.config import settings
from sherlog.detectors.containers import container_detector
from sherlog.detectors.logs import log_detector
from sherlog.detectors.metrics import metric_detector
from sherlog.incidents.manager import manager
from sherlog.investigator import tools
from sherlog.ingest.clients import platform
from sherlog.signals import bus, utcnow
from sherlog.topology.graph import topology

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sherlog")


async def backfill_events() -> None:
    """Pick up change events emitted while Sherlog was down."""
    try:
        events = await platform.events(limit=500)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not backfill change events: %s", exc)
        return
    added = sum([await db.insert_event(e) for e in events])
    log.info("Backfilled %d change events", added)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
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
        )
    ]
    for task in tasks:
        task.add_done_callback(_crash_logger)
    yield
    for task in tasks:
        task.cancel()


def _crash_logger(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("Background task %s crashed", task.get_name(), exc_info=task.exception())


app = FastAPI(title="Sherlog", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"status": "ok", "learning": log_detector.learning, "llm": llm.mode(), "investigate": settings.investigate}


@app.get("/incidents")
async def list_incidents(status: str = "all"):
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
async def get_incident(incident_id: int):
    return _incident(incident_id).to_dict()


@app.post("/incidents/{incident_id}/evidence")
async def rebuild_evidence(incident_id: int):
    return await manager.refresh_evidence(_incident(incident_id))


@app.post("/incidents/{incident_id}/investigate", status_code=202)
async def start_investigation(incident_id: int):
    """Run (or re-run) triage and the investigator on an open incident."""
    incident = _incident(incident_id)
    if not incident.open:
        raise HTTPException(status_code=409, detail=f"incident is {incident.status}")
    if incident.investigating:
        raise HTTPException(status_code=409, detail="already investigating")
    manager.spawn(manager.investigate(incident))
    return {"accepted": True}


@app.get("/tools")
async def list_tools():
    return tools.specs()


@app.post("/tools/{name}")
async def call_tool(name: str, arguments: dict, incident_id: int | None = None):
    """Run an investigator tool by hand (debugging, and the UI's evidence panel).
    Time ranges default to the incident's window, or the last 15 minutes."""
    incident = manager.incidents.get(incident_id) if incident_id else None
    start = (
        datetime.fromisoformat(incident.evidence["window"]["start"])
        if incident and incident.evidence
        else utcnow() - timedelta(minutes=15)
    )
    return await tools.run_tool(tools.ToolContext(incident_id or 0, start), name, arguments)


@app.get("/signals")
async def active_signals():
    return [s.to_dict() for s in bus.active.values()]


@app.get("/topology")
async def get_topology():
    data = topology.to_dict()
    unhealthy = {s.service for s in bus.active.values()}
    for node in data["nodes"]:
        node["status"] = "anomalous" if node["id"] in unhealthy else "ok"
    return data


@app.get("/detectors/metrics")
async def metric_state():
    return metric_detector.snapshot()


@app.get("/detectors/logs")
async def log_state():
    return {
        "lines_seen": log_detector.lines_seen,
        "templates": len(log_detector.templates),
        "exception_fingerprints": len(log_detector.exceptions),
        "interesting_templates": log_detector.interesting_templates()[:20],
        "interesting_exceptions": log_detector.interesting_exceptions()[:10],
    }


@app.post("/events", status_code=202)
async def receive_event(event: dict):
    """Change events pushed by the deploy platform (deploys, config changes, flags)."""
    if not {"id", "type", "timestamp"} <= event.keys():
        raise HTTPException(status_code=422, detail="event needs id, type and timestamp")
    if await db.insert_event(event):
        broadcaster.publish("change", {"event": event})
    return {"accepted": True}


@app.get("/events")
async def list_events(limit: int = 100):
    return await db.recent_events(limit)


@app.get("/stats")
async def stats():
    incidents = list(manager.incidents.values())
    evidence = [i.evidence["stats"] for i in incidents if i.evidence]
    return {
        "incidents_total": len(incidents),
        "incidents_open": sum(1 for i in incidents if i.open),
        "log_lines_seen": log_detector.lines_seen,
        "log_templates": len(log_detector.templates),
        "exception_fingerprints": len(log_detector.exceptions),
        "funnel": {
            "log_lines_in_windows": sum(s["log_lines_in_window"] for s in evidence),
            "estimated_tokens": sum(s["estimated_tokens"] for s in evidence),
        },
        "llm": {"mode": llm.mode(), "by_model": await db.llm_totals()},
    }


@app.get("/stream")
async def stream():
    return StreamingResponse(
        broadcaster.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
