"""Signals: what detectors report, and the bus that carries them.

A detector calls `bus.fire(...)` when a condition starts and `bus.clear(key)`
when it ends. The bus de-duplicates by key, so detectors can call `fire` on
every tick without flooding the incident manager.
"""

import asyncio
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Signal:
    type: str  # e.g. metric_anomaly, log_silence, not_ready, container_restart, new_log_template, new_exception
    service: str
    severity: str  # info | warning | critical
    started_at: datetime
    details: dict
    key: str
    id: str = field(default_factory=lambda: f"sig_{secrets.token_hex(5)}")
    ended_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        data["ended_at"] = self.ended_at.isoformat() if self.ended_at else None
        return data


class SignalBus:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, Signal]] = asyncio.Queue()
        self.active: dict[str, Signal] = {}

    def fire(
        self,
        key: str,
        type_: str,
        service: str,
        severity: str,
        details: dict,
        started_at: datetime | None = None,
    ) -> Signal:
        existing = self.active.get(key)
        if existing:
            existing.details = details  # keep latest numbers for the UI
            return existing
        signal = Signal(type_, service, severity, started_at or utcnow(), details, key)
        self.active[key] = signal
        self.queue.put_nowait(("fired", signal))
        return signal

    def clear(self, key: str) -> None:
        signal = self.active.pop(key, None)
        if signal:
            signal.ended_at = utcnow()
            self.queue.put_nowait(("cleared", signal))

    def event(self, type_: str, service: str, severity: str, details: dict) -> Signal:
        """A one-shot signal (e.g. a container restart) that starts and ends at once."""
        signal = Signal(type_, service, severity, utcnow(), details, f"{type_}:{service}:{secrets.token_hex(3)}")
        signal.ended_at = signal.started_at
        self.queue.put_nowait(("fired", signal))
        return signal

    def active_for(self, service: str) -> list[Signal]:
        return [s for s in self.active.values() if s.service == service]


bus = SignalBus()
