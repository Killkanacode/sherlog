"""Incident manager: groups signals into incidents and tracks their lifecycle.

A signal joins an open incident when its service is already part of it, or
when it is connected to the incident's services in the dependency graph and
arrives within the grouping window. Only impact signals open incidents;
supporting signals (traffic shifts, log-only patterns) attach to one.

States: detected -> investigating -> mitigating -> verifying -> resolved | escalated
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sherlog import db
from sherlog.api.stream import broadcaster
from sherlog.config import settings
from sherlog.funnel.builder import build_evidence
from sherlog.funnel.heuristics import is_impact
from sherlog.signals import Signal, bus, utcnow
from sherlog.topology.graph import topology

log = logging.getLogger("sherlog.incidents")

OPEN_STATES = ("detected", "investigating", "mitigating", "verifying")
EVIDENCE_DELAY_S = 45
SUPPORT_MEMORY = timedelta(minutes=10)


@dataclass
class Incident:
    id: int
    created_at: datetime
    status: str = "detected"
    services: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    evidence: dict | None = None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None
    last_signal_at: datetime | None = None
    quiet_since: datetime | None = None

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATES

    @property
    def title(self) -> str:
        impact = [s for s in self.signals if is_impact(s)]
        lead = min(impact or self.signals, key=lambda s: s.started_at)
        what = lead.details.get("metric") or lead.type
        return f"{what.replace('_', ' ')} on {lead.service}" + (
            f" (+{len(self.services) - 1} more)" if len(self.services) > 1 else ""
        )

    def add_timeline(self, kind: str, text: str, data: dict | None = None, at: datetime | None = None) -> None:
        self.timeline.append({"ts": (at or utcnow()).isoformat(), "kind": kind, "text": text, "data": data or {}})

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "title": self.title,
            "services": self.services,
            "created_at": self.created_at.isoformat(),
            "updated_at": (self.updated_at or self.created_at).isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "active_signals": sum(1 for s in self.signals if s.active),
            "signal_count": len(self.signals),
            "root_cause_hint": (self.evidence or {}).get("root_cause_hint", {}).get("service"),
        }

    def data(self) -> dict:
        return {
            "signals": [s.to_dict() for s in self.signals],
            "timeline": self.timeline,
            "evidence": self.evidence,
        }

    def to_dict(self) -> dict:
        return {**self.summary(), **self.data()}


class IncidentManager:
    def __init__(self) -> None:
        self.incidents: dict[int, Incident] = {}
        self.unattached: list[Signal] = []  # recent supporting signals with no incident yet
        self._tasks: set[asyncio.Task] = set()  # keep references so tasks aren't garbage-collected

    # ------------------------------------------------------------ lookup
    def open_incidents(self) -> list[Incident]:
        return [i for i in self.incidents.values() if i.open]

    def _match(self, signal: Signal) -> Incident | None:
        for incident in sorted(self.open_incidents(), key=lambda i: i.created_at, reverse=True):
            if signal.service in incident.services:
                return incident
            recent = incident.last_signal_at and signal.started_at - incident.last_signal_at <= timedelta(
                seconds=settings.incident_window_s
            )
            if recent and any(topology.connected(signal.service, s) for s in incident.services):
                return incident
        return None

    # ------------------------------------------------------------ events
    async def _attach(self, incident: Incident, signal: Signal) -> None:
        incident.signals.append(signal)
        if signal.service not in incident.services:
            incident.services.append(signal.service)
        incident.last_signal_at = max(incident.last_signal_at or signal.started_at, signal.started_at)
        incident.quiet_since = None
        incident.add_timeline("signal", f"{signal.service}: {_describe(signal)}", signal.to_dict(), signal.started_at)
        broadcaster.publish("signal", {"incident_id": incident.id, "signal": signal.to_dict()})

    async def _open(self, signal: Signal) -> Incident:
        now = utcnow()
        incident_id = await db.insert_incident("detected", "new incident", [signal.service], now, {})
        incident = Incident(id=incident_id, created_at=now)
        self.incidents[incident_id] = incident
        await self._attach(incident, signal)
        window = timedelta(seconds=settings.incident_window_s)
        for pending in list(self.unattached):
            if signal.started_at - pending.started_at <= window and topology.connected(pending.service, signal.service):
                await self._attach(incident, pending)
                self.unattached.remove(pending)
        incident.add_timeline("status", "Incident detected", {"status": "detected"}, now)
        log.info("Opened incident #%d: %s", incident.id, incident.title)
        task = asyncio.create_task(self._evidence_later(incident))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return incident

    async def on_fired(self, signal: Signal) -> None:
        incident = self._match(signal)
        if incident is None:
            if not is_impact(signal):
                self.unattached.append(signal)
                cutoff = utcnow() - SUPPORT_MEMORY
                self.unattached = [s for s in self.unattached if s.started_at >= cutoff]
                return
            incident = await self._open(signal)
        else:
            await self._attach(incident, signal)
        await self.save(incident)

    async def on_cleared(self, signal: Signal) -> None:
        for incident in self.incidents.values():
            if any(s.id == signal.id for s in incident.signals):
                incident.add_timeline("signal_cleared", f"{signal.service}: {_describe(signal)} cleared", signal.to_dict())
                broadcaster.publish("signal", {"incident_id": incident.id, "signal": signal.to_dict()})
                await self.save(incident)
                return

    async def set_status(self, incident: Incident, status: str, text: str) -> None:
        incident.status = status
        if status in ("resolved", "escalated"):
            incident.resolved_at = utcnow()
        incident.add_timeline("status", text, {"status": status})
        log.info("Incident #%d -> %s: %s", incident.id, status, text)
        await self.save(incident)

    async def save(self, incident: Incident) -> None:
        incident.updated_at = utcnow()
        await db.update_incident(
            incident.id, incident.status, incident.title, incident.services, incident.updated_at,
            incident.resolved_at, incident.data(),
        )
        broadcaster.publish("incident", {"incident": incident.summary()})

    async def refresh_evidence(self, incident: Incident) -> dict:
        incident.evidence = await build_evidence(incident.signals, incident.services)
        stats = incident.evidence["stats"]
        incident.add_timeline(
            "evidence",
            f"Evidence funnel: {stats['log_lines_in_window']:,} log lines -> "
            f"{stats['templates_selected']} templates, {stats['exceptions_selected']} exceptions, "
            f"{stats['traces_selected']} traces -> ~{stats['estimated_tokens']:,} tokens",
            stats,
        )
        await self.save(incident)
        return incident.evidence

    async def _evidence_later(self, incident: Incident) -> None:
        await asyncio.sleep(EVIDENCE_DELAY_S)
        try:
            await self.refresh_evidence(incident)
        except Exception:
            log.exception("Evidence funnel failed for incident #%d", incident.id)

    # ------------------------------------------------------------ loops
    async def run(self) -> None:
        while True:
            kind, signal = await bus.queue.get()
            try:
                if kind == "fired":
                    await self.on_fired(signal)
                else:
                    await self.on_cleared(signal)
            except Exception:
                log.exception("Failed to handle %s signal %s", kind, signal.key)

    async def watchdog(self) -> None:
        """Resolve detected incidents once every signal has been quiet for a while.

        (From Phase 4 on, incidents with an action go through verification instead.)
        """
        while True:
            await asyncio.sleep(10)
            now = utcnow()
            for incident in self.open_incidents():
                if incident.status != "detected":
                    continue
                if any(s.active for s in incident.signals):
                    incident.quiet_since = None
                    continue
                incident.quiet_since = incident.quiet_since or now
                if (now - incident.quiet_since).total_seconds() >= settings.resolve_quiet_s:
                    await self.set_status(incident, "resolved", "All signals back to normal; resolved without action")

    async def load(self) -> None:
        """Close incidents left open by a previous run; detector state didn't survive it."""
        for row in await db.recent_incidents():
            if row["status"] in OPEN_STATES:
                data = row["data"]
                data.setdefault("timeline", []).append(
                    {"ts": utcnow().isoformat(), "kind": "status", "text": "Sherlog restarted; incident closed", "data": {}}
                )
                await db.update_incident(row["id"], "escalated", row["title"], row["services"], utcnow(), utcnow(), data)


def _describe(signal: Signal) -> str:
    d = signal.details
    if signal.type == "metric_anomaly":
        peer = f" to {d['peer']}" if d.get("peer") else ""
        return f"{d['metric']}{peer} {d.get('direction', '')} ({d['value']} vs baseline {d['baseline']})"
    if signal.type in ("new_log_template", "log_spike"):
        return f"{signal.type.replace('_', ' ')} \"{d.get('template', '')[:80]}\" ({d.get('per_minute')}/min)"
    if signal.type in ("new_exception", "exception_spike"):
        return f"{signal.type.replace('_', ' ')} {d.get('exception')}: {d.get('message', '')[:80]}"
    return signal.type.replace("_", " ") + (f" {d}" if d else "")


manager = IncidentManager()
