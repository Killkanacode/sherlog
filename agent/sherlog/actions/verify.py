"""Did the mitigation work?

After an action, watch the incident's services until nothing is wrong with
them for SHERLOG_VERIFY_HEALTHY_S, or give up after SHERLOG_VERIFY_TIMEOUT_S.
"Wrong" is read from the detectors' latest state rather than from signals,
which clear only after a persistence window:
  - an impact metric (errors, latency, saturation...) off its baseline
  - a container down or not ready, or a service gone silent
  - an exception from this incident still being thrown above its usual rate
"""

import asyncio
from collections.abc import Awaitable, Callable

from sherlog.config import settings
from sherlog.detectors.logs import log_detector
from sherlog.detectors.metrics import metric_detector
from sherlog.signals import bus, utcnow

STATE_SIGNALS = ("container_down", "not_ready", "log_silence")


def problems(services: set[str], fingerprints: set[str]) -> list[dict]:
    found = metric_detector.anomalies(services)
    found += [
        {"signal": s.type, "service": s.service}
        for s in bus.active.values()
        if s.type in STATE_SIGNALS and s.service in services
    ]
    for fingerprint in sorted(fingerprints):
        exc = log_detector.exceptions.get(fingerprint)
        if exc is None:
            continue
        rate, base = exc.counter.per_minute, exc.counter.baseline.mean or 0.0
        if rate > max(2 * base, base + 1):
            found.append({"exception": exc.type, "service": exc.service, "fingerprint": fingerprint, "per_minute": rate})
    return found


def describe(found: list[dict]) -> str:
    parts = []
    for p in found[:4]:
        if "metric" in p:
            peer = f" to {p['peer']}" if p.get("peer") else ""
            parts.append(f"{p['metric']}{peer} on {p['service']} at {p['value']:g} (baseline {p['baseline']:g})")
        elif "signal" in p:
            parts.append(f"{p['signal'].replace('_', ' ')} on {p['service']}")
        else:
            parts.append(f"{p['exception']} on {p['service']} at {p['per_minute']}/min")
    more = f" and {len(found) - 4} more" if len(found) > 4 else ""
    return "; ".join(parts) + more


async def verify(
    services: set[str], fingerprints: set[str], on_check: Callable[[list[dict]], Awaitable[None]] | None = None
) -> dict:
    started = utcnow()
    healthy_since = None
    checks = 0
    found: list[dict] = []
    while (utcnow() - started).total_seconds() < settings.verify_timeout_s:
        await asyncio.sleep(settings.verify_poll_s)
        now = utcnow()
        found = problems(services, fingerprints)
        checks += 1
        healthy_since = None if found else healthy_since or now
        if on_check:
            await on_check(found)
        if healthy_since and (now - healthy_since).total_seconds() >= settings.verify_healthy_s:
            return {
                "outcome": "recovered",
                "started_at": started.isoformat(),
                "recovered_at": healthy_since.isoformat(),
                "finished_at": now.isoformat(),
                "seconds": round((healthy_since - started).total_seconds()),
                "checks": checks,
                "remaining": [],
            }
    return {
        "outcome": "not_recovered",
        "started_at": started.isoformat(),
        "recovered_at": None,
        "finished_at": utcnow().isoformat(),
        "seconds": round((utcnow() - started).total_seconds()),
        "checks": checks,
        "remaining": found,
    }
