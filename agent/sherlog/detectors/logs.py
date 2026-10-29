"""Log-based detection: Drain3 templates, exception fingerprints and silence.

Logs are pulled from Loki in small forward pages (every few seconds), so the
detector catches up after a restart. One Drain3 miner per service turns lines
into templates. Per template and per exception fingerprint we keep 5-second
buckets for the last minute plus a per-minute EWMA baseline.

Signals:
  new_log_template   a WARN/ERROR template first seen after learning
  log_spike          a known WARN/ERROR template at >= 5x its usual rate
  new_exception      an exception fingerprint first seen after learning
  exception_spike    a known fingerprint at >= 5x its usual rate
  log_silence        a service logging < 10% of its usual volume
"""

import asyncio
import hashlib
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from sherlog.config import SERVICES, settings
from sherlog.detectors.baseline import Ewma
from sherlog.ingest.clients import loki
from sherlog.signals import bus, utcnow

log = logging.getLogger("sherlog.detectors.logs")

BUCKET_S = 5
BUCKETS_PER_MIN = 60 // BUCKET_S
LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "WARNING": 2, "ERROR": 3, "FATAL": 4, "CRITICAL": 4}
QUERY = '{service_name=~"' + "|".join(SERVICES) + '"}'
INGEST_DELAY_S = 3

FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line \d+, in (?P<func>\S+)')


def _miner() -> TemplateMiner:
    config = TemplateMinerConfig()
    config.drain_sim_th = 0.5
    config.drain_depth = 4
    config.masking_instructions = [
        MaskingInstruction(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "UUID"),
        MaskingInstruction(r"\b[0-9a-f]{16,32}\b", "HEX"),
        MaskingInstruction(r"\bSKU-\d+\b", "SKU"),
        MaskingInstruction(r"\bch_[0-9a-f]+\b", "CHARGE"),
        MaskingInstruction(r"\d+(\.\d+)?ms\b", "DURATION"),
        MaskingInstruction(r"\b\d+(\.\d+)?\b", "NUM"),
    ]
    return TemplateMiner(config=config)


def fingerprint(exc_type: str, stacktrace: str) -> tuple[str, list[str]]:
    """hash(type + innermost 5 frames of our own code), ignoring library frames and line numbers."""
    frames = [
        f"{m['file'].removeprefix('/app/')}:{m['func']}"
        for m in FRAME_RE.finditer(stacktrace)
        if "site-packages" not in m["file"] and not m["file"].startswith("/usr/local/lib/python")
    ]
    own = frames[-5:]
    digest = hashlib.sha1("|".join([exc_type, *own]).encode()).hexdigest()[:12]
    return digest, own


def trim_stack(stacktrace: str, max_lines: int = 14) -> str:
    lines = stacktrace.strip().splitlines()
    return "\n".join(lines if len(lines) <= max_lines else ["..."] + lines[-max_lines:])


@dataclass
class Counter:
    """Occurrences in 5 s buckets over the last minute, plus a per-minute baseline."""

    buckets: deque = field(default_factory=lambda: deque([0] * BUCKETS_PER_MIN, maxlen=BUCKETS_PER_MIN))
    baseline: Ewma = field(default_factory=lambda: Ewma(0.1))
    total: int = 0

    def add(self, n: int = 1) -> None:
        self.buckets[-1] += n
        self.total += n

    def rotate(self) -> None:
        self.buckets.append(0)

    @property
    def per_minute(self) -> int:
        return sum(self.buckets)


@dataclass
class Template:
    id: str
    service: str
    text: str
    level: int
    first_seen: datetime
    learned: bool  # seen during the learning window
    counter: Counter = field(default_factory=Counter)
    examples: deque = field(default_factory=lambda: deque(maxlen=3))
    history: deque = field(default_factory=lambda: deque(maxlen=60))  # (minute, count)


@dataclass
class Exception_:
    fingerprint: str
    service: str
    type: str
    message: str
    frames: list[str]
    stack: str
    version: str
    first_seen: datetime
    learned: bool
    counter: Counter = field(default_factory=Counter)
    trace_ids: deque = field(default_factory=lambda: deque(maxlen=5))


class LogDetector:
    def __init__(self) -> None:
        self.miners: dict[str, TemplateMiner] = {s: _miner() for s in SERVICES}
        self.templates: dict[str, Template] = {}
        self.exceptions: dict[str, Exception_] = {}
        self.volume: dict[str, Counter] = {s: Counter() for s in SERVICES}
        self.cursor: datetime | None = None
        self.learning = True
        self.lines_seen = 0
        self._silence_ticks: dict[str, int] = {s: 0 for s in SERVICES}
        self._quiet_ticks: dict[str, int] = {}
        self._last_rotate = utcnow()
        self._last_minute = utcnow()

    # ------------------------------------------------------------ ingestion
    def _ingest(self, stream: dict, ts_ns: str, line: str) -> None:
        service = stream.get("service_name")
        if service not in self.miners:
            return
        when = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
        level = LEVELS.get((stream.get("severity_text") or stream.get("detected_level") or "INFO").upper(), 1)
        self.lines_seen += 1
        self.volume[service].add()

        result = self.miners[service].add_log_message(line)
        template_id = f"{service}:{result['cluster_id']}"
        template = self.templates.get(template_id)
        if template is None:
            template = Template(template_id, service, result["template_mined"], level, when, self.learning)
            self.templates[template_id] = template
        template.text = result["template_mined"]
        template.level = max(template.level, level)
        template.counter.add()
        if len(template.examples) < 3 or level >= 2:
            template.examples.append({"ts": when.isoformat(), "line": line[:300], "trace_id": stream.get("trace_id")})

        exc_type = stream.get("exception_type")
        stacktrace = stream.get("exception_stacktrace")
        if exc_type and stacktrace:
            digest, frames = fingerprint(exc_type, stacktrace)
            exc = self.exceptions.get(digest)
            if exc is None:
                exc = Exception_(
                    digest,
                    service,
                    exc_type,
                    (stream.get("exception_message") or "")[:200],
                    frames,
                    trim_stack(stacktrace),
                    stream.get("service_version", "?"),
                    when,
                    self.learning,
                )
                self.exceptions[digest] = exc
            exc.counter.add()
            if stream.get("trace_id"):
                exc.trace_ids.append(stream["trace_id"])

    async def _pull(self, start: datetime, end: datetime) -> int:
        """Read [start, end) page by page; returns the number of lines ingested."""
        ingested = 0
        while start < end:
            streams = await loki.query_range(QUERY, start, end, limit=5000, direction="forward")
            entries = sorted(
                ((ts, stream["stream"], line) for stream in streams for ts, line in stream["values"]),
                key=lambda entry: int(entry[0]),
            )
            for ts, stream, line in entries:
                self._ingest(stream, ts, line)
            ingested += len(entries)
            if len(entries) < 5000:
                break
            start = datetime.fromtimestamp(int(entries[-1][0]) / 1e9, tz=timezone.utc) + timedelta(microseconds=1)
        return ingested

    # ------------------------------------------------------------ detection
    def _rotate(self) -> None:
        for counter in [t.counter for t in self.templates.values()] + [e.counter for e in self.exceptions.values()] + list(
            self.volume.values()
        ):
            counter.rotate()

    def _close_minute(self, now: datetime) -> None:
        """Fold the last minute into baselines (unless the series is currently alerting)."""
        for template in self.templates.values():
            template.history.append((now.isoformat(), template.counter.per_minute))
            if not self._is_alerting(f"logtpl:{template.id}") and not self._is_alerting(f"logspike:{template.id}"):
                template.counter.baseline.update(template.counter.per_minute)
        for exc in self.exceptions.values():
            if not self._is_alerting(f"exc:{exc.fingerprint}"):
                exc.counter.baseline.update(exc.counter.per_minute)
        for service, counter in self.volume.items():
            if not self._is_alerting(f"silence:{service}"):
                counter.baseline.update(counter.per_minute)

    @staticmethod
    def _is_alerting(key: str) -> bool:
        return key in bus.active

    def _evaluate(self) -> None:
        for template in self.templates.values():
            if template.level < 2:
                continue
            rate = template.counter.per_minute
            base = template.counter.baseline.mean or 0.0
            if not template.learned:
                self._toggle(
                    f"logtpl:{template.id}",
                    rate >= 3,
                    rate == 0,
                    "new_log_template",
                    template.service,
                    "critical" if template.level >= 3 else "warning",
                    {"template": template.text, "level": "ERROR" if template.level >= 3 else "WARN", "per_minute": rate,
                     "template_id": template.id},
                )
            else:
                self._toggle(
                    f"logspike:{template.id}",
                    rate >= max(5 * base, base + 20),
                    rate <= max(2 * base, base + 5),
                    "log_spike",
                    template.service,
                    "warning",
                    {"template": template.text, "per_minute": rate, "baseline_per_minute": round(base, 1),
                     "template_id": template.id},
                )
        for exc in self.exceptions.values():
            rate = exc.counter.per_minute
            base = exc.counter.baseline.mean or 0.0
            spiking = rate >= max(5 * base, base + 10)
            self._toggle(
                f"exc:{exc.fingerprint}",
                (rate >= 2) if not exc.learned else spiking,
                rate == 0 if not exc.learned else rate <= max(2 * base, base + 3),
                "new_exception" if not exc.learned else "exception_spike",
                exc.service,
                "critical",
                {"fingerprint": exc.fingerprint, "exception": exc.type, "message": exc.message,
                 "frames": exc.frames, "version": exc.version, "per_minute": rate},
            )
        for service, counter in self.volume.items():
            base = counter.baseline.mean or 0.0
            silent = base >= 30 and counter.per_minute < 0.1 * base
            self._silence_ticks[service] = self._silence_ticks[service] + 1 if silent else 0
            if self._silence_ticks[service] >= 2 * settings.persistence:  # about 30 s of near-silence
                bus.fire(f"silence:{service}", "log_silence", service, "critical",
                         {"per_minute": counter.per_minute, "baseline_per_minute": round(base, 1)})
            elif not silent and counter.per_minute >= 0.5 * base:
                bus.clear(f"silence:{service}")

    def _toggle(self, key: str, fire: bool, clear: bool, type_: str, service: str, severity: str, details: dict) -> None:
        if fire:
            self._quiet_ticks.pop(key, None)
            bus.fire(key, type_, service, severity, details)
        elif key in bus.active:
            if clear:
                self._quiet_ticks[key] = self._quiet_ticks.get(key, 0) + 1
                if self._quiet_ticks[key] >= 12:  # a quiet minute
                    self._quiet_ticks.pop(key, None)
                    bus.clear(key)
            else:
                self._quiet_ticks.pop(key, None)
                bus.active[key].details = details

    # ------------------------------------------------------------ loop
    async def learn(self) -> None:
        end = utcnow() - timedelta(seconds=INGEST_DELAY_S)
        start = end - timedelta(seconds=settings.learning_s)
        # Walk history minute by minute so baselines see realistic per-minute counts.
        cursor = start
        while cursor < end:
            step_end = min(cursor + timedelta(minutes=1), end)
            try:
                await self._pull(cursor, step_end)
            except Exception as exc:  # noqa: BLE001
                log.warning("Learning pull failed: %s", exc)
            for _ in range(BUCKETS_PER_MIN):
                self._rotate()
            self._close_minute(step_end)
            cursor = step_end
        self.cursor = end
        self.learning = False
        log.info(
            "Learned %d templates and %d exception fingerprints from %d lines",
            len(self.templates), len(self.exceptions), self.lines_seen,
        )

    async def run(self) -> None:
        await self.learn()
        while True:
            await asyncio.sleep(settings.log_poll_s)
            now = utcnow()
            end = now - timedelta(seconds=INGEST_DELAY_S)
            try:
                await self._pull(self.cursor, end)
                self.cursor = end
            except Exception as exc:  # noqa: BLE001
                log.warning("Log pull failed: %s", exc)
                continue
            if (now - self._last_rotate).total_seconds() >= BUCKET_S:
                self._rotate()
                self._last_rotate = now
            if (now - self._last_minute).total_seconds() >= 60:
                self._close_minute(now)
                self._last_minute = now
            self._evaluate()

    # ------------------------------------------------------------ queries
    def interesting_templates(self, services: set[str] | None = None) -> list[dict]:
        """WARN/ERROR templates that are new or above their usual rate, for the evidence funnel."""
        rows = []
        for template in self.templates.values():
            if services and template.service not in services:
                continue
            base = template.counter.baseline.mean or 0.0
            recent = sum(count for _, count in list(template.history)[-10:]) + template.counter.per_minute
            new = not template.learned
            spiking = recent > 0 and template.counter.per_minute >= max(3 * base, base + 10)
            if template.level >= 2 and (new or spiking) or (new and recent >= 20):
                rows.append(
                    {
                        "id": template.id,
                        "service": template.service,
                        "template": template.text,
                        "level": "ERROR" if template.level >= 3 else "WARN" if template.level == 2 else "INFO",
                        "new": new,
                        "first_seen": template.first_seen.isoformat(),
                        "per_minute": template.counter.per_minute,
                        "baseline_per_minute": round(base, 1),
                        "count_10m": recent,
                        "examples": list(template.examples),
                    }
                )
        return sorted(rows, key=lambda r: (-int(r["new"]), -r["count_10m"]))

    def interesting_exceptions(self, services: set[str] | None = None) -> list[dict]:
        rows = []
        for exc in self.exceptions.values():
            if services and exc.service not in services:
                continue
            base = exc.counter.baseline.mean or 0.0
            if exc.learned and exc.counter.per_minute < max(3 * base, base + 5):
                continue
            rows.append(
                {
                    "fingerprint": exc.fingerprint,
                    "service": exc.service,
                    "type": exc.type,
                    "message": exc.message,
                    "new": not exc.learned,
                    "first_seen": exc.first_seen.isoformat(),
                    "per_minute": exc.counter.per_minute,
                    "total": exc.counter.total,
                    "version": exc.version,
                    "frames": exc.frames,
                    "stack": exc.stack,
                    "trace_ids": list(exc.trace_ids),
                }
            )
        return sorted(rows, key=lambda r: -r["total"])


log_detector = LogDetector()
