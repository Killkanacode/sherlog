"""Incident manager: groups signals into incidents and drives their lifecycle.

A signal joins a live incident when its service is already part of it, or
when it is connected to the incident's services in the dependency graph and
arrives within the grouping window. Only impact signals open incidents;
supporting signals (traffic shifts, log-only patterns) attach to one.

States: detected -> investigating -> mitigating -> verifying -> resolved | escalated

After the diagnosis, the recommended action is only suggested, waits for a
human's approval or runs on its own, depending on the autonomy level. An
action that ran is verified; if the services don't recover it is reverted and
the investigator goes again knowing it failed, up to SHERLOG_MAX_ACTION_CYCLES
times before escalating. An escalated incident keeps collecting its signals
until they clear. Closing an incident writes its postmortem and stores it in
the incident memory.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sherlog import db, llm
from sherlog.actions import execute as actions
from sherlog.actions import verify
from sherlog.actions.policy import policy, throttle, vet
from sherlog.api.stream import broadcaster
from sherlog.config import settings
from sherlog.detectors.logs import log_detector
from sherlog.funnel.builder import build_evidence
from sherlog.funnel.heuristics import is_impact
from sherlog.investigator import agent as investigator
from sherlog.investigator.triage import run_triage
from sherlog.llm.redact import looks_like_injection, scan_evidence
from sherlog.llm.usage import Usage
from sherlog.memory import store as memory
from sherlog.postmortem import writer as postmortem
from sherlog.signals import Signal, bus, describe, utcnow
from sherlog.topology.graph import topology

log = logging.getLogger("sherlog.incidents")

OPEN_STATES = ("detected", "investigating", "mitigating", "verifying")
EVIDENCE_DELAY_S = 45
SUPPORT_MEMORY = timedelta(minutes=10)

# Action states: proposed -> suggested | awaiting_approval -> approved -> executing -> verifying
#   -> recovered | reverting -> reverted | not_recovered; or rejected, superseded, expired, simulated, error.
PENDING_ACTION_STATES = ("suggested", "awaiting_approval")
BUSY_ACTION_STATES = ("approved", "executing", "verifying", "reverting")
FAILED_ACTION_STATES = ("reverted", "not_recovered")


class ActionError(Exception):
    """An action request that doesn't fit the incident's state."""


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
    triage: dict | None = None
    diagnosis: dict | None = None
    investigation: dict | None = None  # {"state": running|done|failed, "mode": fake|live, ...}
    usage: Usage | None = None
    actions: list[dict] = field(default_factory=list)
    seen_before: list[dict] = field(default_factory=list)
    postmortem: str | None = None
    untrusted: str | None = None  # instruction-like text found in its telemetry

    def __post_init__(self) -> None:
        self.usage = self.usage or Usage(self.id)

    @property
    def investigating(self) -> bool:
        return bool(self.investigation and self.investigation["state"] in ("running", "queued"))

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATES

    @property
    def live(self) -> bool:
        """Still collecting signals: open, or escalated and not over yet."""
        return self.resolved_at is None

    @property
    def busy(self) -> bool:
        return self.investigating or any(a["state"] in BUSY_ACTION_STATES for a in self.actions)

    @property
    def pending_action(self) -> dict | None:
        return next((a for a in self.actions if a["state"] in PENDING_ACTION_STATES), None)

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
            "root_cause": (self.diagnosis or {}).get("root_cause"),
            "investigation": self.investigation,
            "pending_action": self.pending_action,
            "seen_before": self.seen_before[0] if self.seen_before else None,
            "has_postmortem": self.postmortem is not None,
            "llm_usage": self.usage.totals() if self.usage.calls else None,
        }

    def data(self) -> dict:
        return {
            "signals": [s.to_dict() for s in self.signals],
            "timeline": self.timeline,
            "evidence": self.evidence,
            "triage": self.triage,
            "diagnosis": self.diagnosis,
            "actions": self.actions,
            "past_incidents": self.seen_before,  # every memory match; summary's seen_before is the best one
            "postmortem": self.postmortem,
            "untrusted_content": self.untrusted,
            "llm_calls": self.usage.calls,
        }

    def to_dict(self) -> dict:
        return {**self.summary(), **self.data()}


class IncidentManager:
    def __init__(self) -> None:
        self.incidents: dict[int, Incident] = {}
        self.unattached: list[Signal] = []  # recent supporting signals with no incident yet
        self._tasks: set[asyncio.Task] = set()  # keep references so tasks aren't garbage-collected
        # Investigations call the models; cap how many run at once so an
        # incident storm queues instead of multiplying cost and rate limits.
        self._slots = asyncio.Semaphore(settings.max_concurrent_investigations)

    # ------------------------------------------------------------ lookup
    def open_incidents(self) -> list[Incident]:
        return [i for i in self.incidents.values() if i.open]

    def live_incidents(self) -> list[Incident]:
        return [i for i in self.incidents.values() if i.live]

    def _match(self, signal: Signal) -> Incident | None:
        for incident in sorted(self.live_incidents(), key=lambda i: i.created_at, reverse=True):
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
        incident.add_timeline("signal", f"{signal.service}: {describe(signal)}", signal.to_dict(), signal.started_at)
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
        self.spawn(self._evidence_later(incident))
        return incident

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
                incident.add_timeline("signal_cleared", f"{signal.service}: {describe(signal)} cleared", signal.to_dict())
                broadcaster.publish("signal", {"incident_id": incident.id, "signal": signal.to_dict()})
                await self.save(incident)
                return

    async def set_status(self, incident: Incident, status: str, text: str) -> None:
        incident.status = status
        incident.add_timeline("status", text, {"status": status})
        log.info("Incident #%d -> %s: %s", incident.id, status, text)
        await self.save(incident)

    async def escalate(self, incident: Incident, reason: str) -> None:
        await self.set_status(incident, "escalated", f"Escalated to a human: {reason}")

    async def close(self, incident: Incident, text: str, recovered_at: datetime | None = None) -> None:
        """Resolve the incident (an escalated one stays escalated), then write
        its postmortem and remember it."""
        now = utcnow()
        for action in incident.actions:
            if action["state"] in PENDING_ACTION_STATES:
                action["state"] = "expired"
        incident.resolved_at = now
        if incident.status != "escalated":
            incident.status = "resolved"
        incident.add_timeline("status", text, {"status": incident.status})
        log.info("Incident #%d closed (%s): %s", incident.id, incident.status, text)
        await self.save(incident)
        start = min(s.started_at for s in incident.signals)
        recovered_at = recovered_at or now
        try:
            impact = await postmortem.gateway_impact(start, recovered_at)
            incident.postmortem = postmortem.write(incident, start, recovered_at, impact)
            await memory.remember(incident)
        except Exception:
            log.exception("Postmortem or memory failed for incident #%d", incident.id)
        await self.save(incident)
        if incident.postmortem:
            broadcaster.publish("postmortem", {"incident_id": incident.id})

    async def save(self, incident: Incident) -> None:
        incident.updated_at = utcnow()
        await db.update_incident(
            incident.id, incident.status, incident.title, incident.services, incident.updated_at,
            incident.resolved_at, incident.data(),
        )
        broadcaster.publish("incident", {"incident": incident.summary()})

    async def refresh_evidence(self, incident: Incident) -> dict:
        evidence = await build_evidence(incident.signals, incident.services)
        try:
            evidence["past_incidents"] = await memory.recall(incident.id, evidence)
        except Exception:
            log.exception("Memory recall failed for incident #%d", incident.id)
            evidence["past_incidents"] = []
        incident.evidence = evidence
        hit = scan_evidence(evidence)
        if not hit:
            line = log_detector.suspicious_in(set(incident.services), datetime.fromisoformat(evidence["window"]["start"]))
            hit = line and line["match"]
        if hit and not incident.untrusted:
            self._flag_untrusted(incident, hit)
        stats = evidence["stats"]
        incident.add_timeline(
            "evidence",
            f"Evidence funnel: {stats['log_lines_in_window']:,} log lines -> "
            f"{stats['templates_selected']} templates, {stats['exceptions_selected']} exceptions, "
            f"{stats['traces_selected']} traces -> ~{stats['estimated_tokens']:,} tokens",
            stats,
        )
        known = {m["incident_id"] for m in incident.seen_before}
        new = [m for m in evidence["past_incidents"] if m["incident_id"] not in known]
        if evidence["past_incidents"]:
            incident.seen_before = evidence["past_incidents"]
        if new:
            incident.add_timeline("seen_before", f"Seen before: {memory.describe(new[0])}", {"matches": new})
        await self.save(incident)
        return evidence

    async def _evidence_later(self, incident: Incident) -> None:
        await asyncio.sleep(EVIDENCE_DELAY_S)
        try:
            await self.refresh_evidence(incident)
        except Exception:
            log.exception("Evidence funnel failed for incident #%d", incident.id)
            return
        if settings.investigate and incident.open:
            await self.investigate(incident)

    @staticmethod
    def _flag_untrusted(incident: Incident, hit: str) -> None:
        incident.untrusted = hit
        incident.add_timeline(
            "security",
            f'Telemetry contains text that reads like instructions to an AI ("{hit[:80]}"). '
            "It is treated as data, and no action will run without a human's approval.",
            {"match": hit},
        )

    # ------------------------------------------------------------ investigation
    async def investigate(self, incident: Incident, refresh: bool = False) -> None:
        """Triage, then the investigator, then act on the diagnosis. `refresh`
        rebuilds the evidence first (after a mitigation that didn't help).
        Waits for a free slot when too many investigations are running."""
        if incident.busy:
            return
        if self._slots.locked():
            incident.investigation = {"state": "queued", "mode": llm.mode(), "queued_at": utcnow().isoformat()}
            incident.add_timeline(
                "status", f"Queued: {settings.max_concurrent_investigations} investigations are already running"
            )
            await self.save(incident)
        async with self._slots:
            if not incident.live:
                return
            incident.investigation = None
            await self._investigate(incident, refresh)

    async def _investigate(self, incident: Incident, refresh: bool) -> None:
        if incident.busy:
            return
        incident.investigation = {"state": "running", "mode": llm.mode(), "started_at": utcnow().isoformat()}
        attempts = self._attempts(incident)
        again = " again, knowing the last mitigation failed" if attempts else ""
        await self.set_status(incident, "investigating", f"Investigation started{again} ({llm.mode()} models)")

        async def emit(kind: str, text: str, data: dict) -> None:
            incident.add_timeline(kind, text, data)
            broadcaster.publish("investigation", {"incident_id": incident.id, "kind": kind, "text": text, "data": data})
            await self.save(incident)

        try:
            evidence = await self.refresh_evidence(incident) if refresh or not incident.evidence else incident.evidence
            incident.triage = await run_triage(llm.triage_model(), evidence, incident.usage)
            if incident.triage:
                await emit("triage", incident.triage["overview"], incident.triage)
            incident.diagnosis = await investigator.investigate(
                llm.investigator_model(), incident.summary(), evidence, incident.triage, incident.usage, emit, attempts
            )
        except Exception as exc:  # noqa: BLE001 - an unreachable model must not take the incident down
            log.exception("Investigation failed for incident #%d", incident.id)
            incident.investigation.update(state="failed", error=f"{type(exc).__name__}: {exc}", finished_at=utcnow().isoformat())
            await emit("investigation_failed", f"Investigation failed: {exc}", {})
            if attempts:
                await self.escalate(incident, "the investigation failed after a mitigation that didn't help")
            else:
                await self.set_status(incident, "detected", "Back to detection only; resolves when the signals clear")
            return
        if not incident.untrusted:
            for entry in incident.timeline:
                hit = entry["kind"] == "tool" and looks_like_injection(str(entry["data"].get("result", "")))
                if hit:
                    self._flag_untrusted(incident, hit)
                    break
        cause = incident.diagnosis["root_cause"]
        totals = incident.usage.totals()
        incident.investigation.update(
            state="done", finished_at=utcnow().isoformat(),
            steps=sum(1 for c in incident.usage.calls if c["role"] == "investigator"),
        )
        cost = f"${totals['cost_usd']:.4f}" if totals["cost_usd"] is not None else "cost unknown"
        await emit(
            "diagnosis",
            f"Root cause: {cause['service']} ({cause['category']}, confidence {cause['confidence']:.2f}). "
            f"{totals['total_tokens']:,} tokens, {cost}",
            incident.diagnosis,
        )
        await self.respond(incident)

    @staticmethod
    def _attempts(incident: Incident) -> list[str]:
        """Mitigations that ran and didn't restore service, for the investigator."""
        lines = []
        for action in incident.actions:
            if action["state"] not in FAILED_ACTION_STATES:
                continue
            check = action.get("verification") or {}
            lines.append(
                f"{action['description']}: services still unhealthy {check.get('seconds', '?')} s later "
                f"({verify.describe(check.get('remaining') or []) or 'no details'}); "
                + ("reverted" if action["state"] == "reverted" else "left in place")
            )
        return lines

    # ------------------------------------------------------------ actions
    async def respond(self, incident: Incident) -> None:
        """Turn the diagnosis into an action, as far as the autonomy level allows."""
        cause = incident.diagnosis["root_cause"]
        recommended = incident.diagnosis["recommended_action"]
        type_ = recommended["type"]
        params = {k: v for k, v in recommended["params"].items() if v is not None}
        if type_ == "escalate":
            return await self.escalate(
                incident,
                f"the investigator recommends it (root cause {cause['service']} / {cause['category']}, "
                f"confidence {cause['confidence']:.2f})",
            )
        if type_ == "open_fix_pr":
            return await self.escalate(incident, "the investigator wants a code fix and the fixer isn't available yet")
        refused = actions.check(type_, params)
        if refused:
            return await self.escalate(incident, f"the recommended action can't run: {refused}")
        if any(a["type"] == type_ and a["params"] == params for a in incident.actions if a["state"] in FAILED_ACTION_STATES):
            return await self.escalate(
                incident, f"the investigator recommended again to {actions.describe(type_, params)}, which already failed"
            )
        unfit = vet(incident.evidence, incident.services, type_, params)
        if unfit:
            return await self.escalate(incident, f"the recommended action failed a safety check: {unfit}")
        action = self._new_action(incident, type_, params, actions.risk(type_, params, recommended["risk"]), "sherlog")
        decision = policy.decide(action, cause["confidence"])
        held = ""
        if decision == "auto":
            now = utcnow()
            recent = [
                a for i in self.incidents.values() for a in i.actions
                if a.get("executed_at") and now - datetime.fromisoformat(a["executed_at"]) < timedelta(hours=1)
            ]
            reason = (
                "the evidence contains instruction-like text" if incident.untrusted else throttle(recent, params.get("service"), now)
            )
            if reason:
                decision, held = "approve", f"; not automatic because {reason}"
        incident.status = "mitigating"
        if decision == "suggest":
            action["state"] = "suggested"
            await self._action_event(incident, action, f"Suggested: {action['description']} (autonomy L0: Sherlog won't act)")
        elif decision == "approve":
            action["state"] = "awaiting_approval"
            await self._action_event(
                incident, action, f"Awaiting approval: {action['description']} (risk {action['risk']}{held})"
            )
        else:
            action.update(state="approved", approved_by="sherlog", approved_at=utcnow().isoformat())
            await self._action_event(
                incident, action,
                f"Auto-approved: {action['description']} (autonomy L2, risk {action['risk']}, "
                f"confidence {cause['confidence']:.2f})",
            )
            await self.execute(incident, action)

    async def approve(self, incident: Incident, actor: str) -> dict:
        action = incident.pending_action
        if action is None or action["state"] != "awaiting_approval":
            raise ActionError("no action is awaiting approval")
        action.update(state="approved", approved_by=actor, approved_at=utcnow().isoformat())
        await self._action_event(incident, action, f"Approved by {actor}: {action['description']}")
        self.spawn(self.execute(incident, action))
        return action

    async def reject(self, incident: Incident, actor: str, reason: str | None) -> dict:
        action = incident.pending_action
        if action is None:
            raise ActionError("no action is pending")
        action.update(state="rejected", rejected_by=actor, rejected_reason=reason)
        await self._action_event(
            incident, action, f"Rejected by {actor}: {action['description']}" + (f" ({reason})" if reason else "")
        )
        await self.escalate(incident, f"{actor} rejected the proposed action")
        return action

    async def run_action(self, incident: Incident, type_: str, params: dict, actor: str) -> dict:
        """An action a human picked (e.g. after an escalation). Their request is the approval."""
        if not incident.live:
            raise ActionError(f"incident is {incident.status}")
        if incident.busy:
            raise ActionError("Sherlog is already working on this incident")
        refused = actions.check(type_, params)
        if refused:
            raise ActionError(refused)
        action = self._new_action(incident, type_, params, actions.risk(type_, params, "low"), actor)
        action.update(state="approved", approved_by=actor, approved_at=utcnow().isoformat())
        await self._action_event(incident, action, f"{actor} asked Sherlog to {action['description']}")
        self.spawn(self.execute(incident, action))
        return action

    @staticmethod
    def _new_action(incident: Incident, type_: str, params: dict, risk: str, proposed_by: str) -> dict:
        for older in incident.actions:
            if older["state"] in PENDING_ACTION_STATES:
                older["state"] = "superseded"
        action = {
            "id": len(incident.actions) + 1,
            "type": type_,
            "params": params,
            "risk": risk,
            "description": actions.describe(type_, params),
            "state": "proposed",
            "proposed_by": proposed_by,
            "proposed_at": utcnow().isoformat(),
            "approved_by": None,
            "approved_at": None,
            "executed_at": None,
            "dry_run": False,
            "result": None,
            "undo": None,
            "verification": None,
            "error": None,
        }
        incident.actions.append(action)
        return action

    async def _action_event(self, incident: Incident, action: dict, text: str) -> None:
        incident.add_timeline("action", text, {"action": dict(action), "status": incident.status})
        broadcaster.publish("action", {"incident_id": incident.id, "action": action})
        log.info("Incident #%d action %d (%s): %s", incident.id, action["id"], action["state"], text)
        await self.save(incident)

    async def execute(self, incident: Incident, action: dict) -> None:
        """Run an approved action, verify recovery, and revert and re-investigate if it didn't help."""
        if policy.dry_run:
            action.update(state="simulated", dry_run=True)
            await self._action_event(incident, action, f"Dry run: would {action['description']}; nothing was changed")
            return
        services = set(incident.services) | ({action["params"]["service"]} if action["params"].get("service") else set())
        fingerprints = {
            s.details["fingerprint"] for s in incident.signals if s.type in ("new_exception", "exception_spike")
        }
        before = verify.problems(services, fingerprints)
        action["state"] = "executing"
        incident.status = "mitigating"
        await self._action_event(incident, action, f"Executing: {action['description']}")
        try:
            action["result"], action["undo"] = await actions.execute(action["type"], action["params"])
        except Exception as exc:  # noqa: BLE001 - the platform's refusal is reported, not raised
            action.update(state="error", error=f"{type(exc).__name__}: {exc}")
            await self._action_event(incident, action, f"Could not {action['description']}: {exc}")
            await self.escalate(incident, "the mitigation failed to run")
            return

        action.update(state="verifying", executed_at=utcnow().isoformat())
        incident.status = "verifying"
        await self._action_event(
            incident, action,
            f"Done: {action['description']}. Verifying recovery of {', '.join(sorted(services))} "
            f"(up to {settings.verify_timeout_s} s)",
        )

        async def progress(found: list[dict]) -> None:
            broadcaster.publish(
                "verification", {"incident_id": incident.id, "action_id": action["id"], "problems": found}
            )

        result = await verify.verify(services, fingerprints, progress)
        action["verification"] = {**result, "before": before}
        if result["outcome"] == "recovered":
            action["state"] = "recovered"
            await self._action_event(
                incident, action, f"Verified: back to baseline {result['seconds']} s after the action"
            )
            await self.close(
                incident, f"Resolved by Sherlog: {action['description']}", datetime.fromisoformat(result["recovered_at"])
            )
            return

        text = f"Not recovered after {result['seconds']} s ({verify.describe(result['remaining'])})"
        revert_failed = False
        if action["undo"]:
            action["state"] = "reverting"
            try:
                action["revert_result"] = await actions.revert(action["undo"])
                action["state"] = "reverted"
                text += f". Reverted: {action['undo']['description']}"
            except actions.Superseded as exc:
                action["state"] = "not_recovered"
                text += f". {str(exc)[0].upper()}{str(exc)[1:]}"
            except Exception as exc:  # noqa: BLE001
                action.update(state="not_recovered", error=f"revert failed: {exc}")
                text += f". Could not revert ({exc})"
                revert_failed = True
        else:
            action["state"] = "not_recovered"
            text += ". It can't be undone, so it stays in place"
        await self._action_event(incident, action, text)
        failed = sum(1 for a in incident.actions if a["state"] in FAILED_ACTION_STATES)
        if revert_failed:
            await self.escalate(incident, "a mitigation didn't help and couldn't be reverted")
        elif failed >= settings.max_action_cycles:
            await self.escalate(incident, f"{failed} mitigations didn't restore service")
        else:
            await self.investigate(incident, refresh=True)

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
        """Close live incidents once every signal has been quiet for a while,
        unless Sherlog is investigating or running an action on them. (An action
        that ran closes its incident through verification instead.)"""
        while True:
            await asyncio.sleep(10)
            now = utcnow()
            for incident in self.live_incidents():
                if incident.busy or any(s.active for s in incident.signals):
                    incident.quiet_since = None
                    continue
                incident.quiet_since = incident.quiet_since or now
                if (now - incident.quiet_since).total_seconds() < settings.resolve_quiet_s:
                    continue
                if incident.status == "escalated":
                    text = "All signals back to normal after the escalation"
                elif any(a["state"] == "simulated" for a in incident.actions):
                    text = "All signals back to normal; the action was a dry run, so nothing was changed"
                else:
                    text = "All signals back to normal; resolved without action"
                try:
                    await self.close(incident, text, incident.quiet_since)
                except Exception:
                    log.exception("Could not close incident #%d", incident.id)

    async def load(self) -> None:
        """Close incidents left live by a previous run; detector state didn't survive it."""
        for row in await db.recent_incidents():
            if row["resolved_at"] is None and (row["status"] in OPEN_STATES or row["status"] == "escalated"):
                data = row["data"]
                data.setdefault("timeline", []).append(
                    {"ts": utcnow().isoformat(), "kind": "status", "text": "Sherlog restarted; incident closed", "data": {}}
                )
                await db.update_incident(row["id"], "escalated", row["title"], row["services"], utcnow(), utcnow(), data)


manager = IncidentManager()
