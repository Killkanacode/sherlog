"""Log-based detection: Drain3 templates, exception fingerprints and silence.

Sherlog never reads every log line. Every few seconds it asks Loki for two
things: how many lines each service wrote (a count computed inside Loki),
and the WARN/ERROR/FATAL lines themselves, filtered inside Loki too. In a
healthy system that is a handful of lines out of tens of thousands, so the
cost follows the error volume, not the total volume. If a poll still brings
more than MAX_LINES_PER_POLL lines, the rest of the window is skipped and
counted as dropped rather than letting the detector fall behind.

One Drain3 miner per service turns WARN+ lines into templates. Per template
and per exception fingerprint we keep 5-second buckets for the last minute
plus a per-minute EWMA baseline. The whole state is snapshotted to Postgres
every minute, so a restart resumes where it stopped instead of relearning.

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

import jsonpickle
from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

from sherlog import db
from sherlog.config import SERVICES, settings
from sherlog.detectors.baseline import Ewma
from sherlog.ingest.clients import loki
from sherlog.llm.redact import looks_like_injection
from sherlog.signals import bus, utcnow

log = logging.getLogger("sherlog.detectors.logs")

BUCKET_S = 5
BUCKETS_PER_MIN = 60 // BUCKET_S
LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "WARNING": 2, "ERROR": 3, "FATAL": 4, "CRITICAL": 4}
QUERY = '{service_name=~"' + "|".join(SERVICES) + '"}'
WARN_QUERY = QUERY + ' | detected_level=~"(?i)(warn|warning|error|fatal|critical)"'
INGEST_DELAY_S = 3
MAX_LINES_PER_POLL = 20_000
PAGE = 5000
GRADUATE_AFTER = timedelta(hours=6)  # a template or fingerprint around this long is no longer "new"
SNAPSHOT_KEY = "log_detector"
SNAPSHOT_EVERY_S = 60
SILENCE_ADAPT = timedelta(minutes=10)
VOLUME_BURST_FLOOR = 600  # lines/min a quiet service's volume baseline may still grow by per minute

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
        self.lines_seen = 0  # every line the services wrote (counted by Loki)
        self.lines_parsed = 0  # WARN+ lines Sherlog read and mined
        self.lines_dropped = 0  # WARN+ lines skipped when a poll overflowed
        self.restored = False
        # WARN+ lines that read like instructions to an AI, whether or not their
        # template is new: a repeated injection must not slip through.
        self.suspicious: deque = deque(maxlen=100)
        self._last_snapshot = utcnow()
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
        self.lines_parsed += 1

        result = self.miners[service].add_log_message(line)
        template_id = f"{service}:{result['cluster_id']}"
        template = self.templates.get(template_id)
        if template is None:
            template = Template(template_id, service, result["template_mined"], level, when, self.learning)
            self.templates[template_id] = template
        template.text = result["template_mined"]
        template.level = max(template.level, level)
        template.counter.add()
        hit = looks_like_injection(line)
        if hit:
            self.suspicious.append({"ts": when, "service": service, "match": hit, "line": line[:200]})
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

    async def _count(self, start: datetime, end: datetime) -> int:
        """Lines per service in [start, end), counted inside Loki."""
        window_ms = max(1, int((end - start).total_seconds() * 1000))
        result = await loki.query(f"sum by (service_name) (count_over_time({QUERY}[{window_ms}ms]))", at=end)
        total = 0
        for row in result:
            service = row["metric"].get("service_name")
            if service in self.volume:
                n = int(float(row["value"][1]))
                self.volume[service].add(n)
                total += n
        self.lines_seen += total
        return total

    async def _pull(self, start: datetime, end: datetime) -> int:
        """Count [start, end) and read its WARN+ lines page by page; returns the lines parsed."""
        await self._count(start, end)
        parsed = 0
        while start < end:
            streams = await loki.query_range(WARN_QUERY, start, end, limit=PAGE, direction="forward")
            entries = sorted(
                ((ts, stream["stream"], line) for stream in streams for ts, line in stream["values"]),
                key=lambda entry: int(entry[0]),
            )
            for ts, stream, line in entries:
                self._ingest(stream, ts, line)
            parsed += len(entries)
            if len(entries) < PAGE:
                break
            start = datetime.fromtimestamp(int(entries[-1][0]) / 1e9, tz=timezone.utc) + timedelta(microseconds=1)
            if parsed >= MAX_LINES_PER_POLL:
                # Don't fall behind: count what is left and move on. Rates stay
                # far above any threshold, so spikes are still detected.
                left = await loki.query(
                    f"sum(count_over_time({WARN_QUERY}[{max(1, int((end - start).total_seconds() * 1000))}ms]))", at=end
                )
                self.lines_dropped += int(float(left[0]["value"][1])) if left else 0
                break
        return parsed

    # ------------------------------------------------------------ detection
    def _counters(self) -> list[Counter]:
        return [*self.volume.values(), *(t.counter for t in self.templates.values()), *(e.counter for e in self.exceptions.values())]

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
            silence = bus.active.get(f"silence:{service}")
            # A silence alert freezes the baseline, but not forever: after
            # SILENCE_ADAPT, the lower volume is accepted as the new normal.
            if silence is None or now - silence.started_at >= SILENCE_ADAPT:
                # Winsorized: a burst counts as at most 3x the usual volume, so a
                # few minutes of log flood can't make normal volume look like silence.
                mean = counter.baseline.mean
                cap = None if mean is None else max(3 * mean, mean + VOLUME_BURST_FLOOR)
                counter.baseline.update(counter.per_minute if cap is None else min(counter.per_minute, cap))
        for item in [*self.templates.values(), *self.exceptions.values()]:
            if not item.learned and now - item.first_seen >= GRADUATE_AFTER:
                item.learned = True  # around for hours: part of normal now, alert on spikes only

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
        minutes: dict[int, list[int]] = {}
        counters: dict[int, Counter] = {}
        learned = 0
        while cursor < end:
            step_end = min(cursor + timedelta(minutes=1), end)
            try:
                await self._pull(cursor, step_end)
            except Exception as exc:  # noqa: BLE001
                log.warning("Learning pull failed: %r", exc)
            for counter in self._counters():
                counters[id(counter)] = counter
                minutes.setdefault(id(counter), [0] * learned).append(counter.per_minute)
            learned += 1
            self._close_minute(step_end)  # fold this minute in, then start the next one empty
            for _ in range(BUCKETS_PER_MIN):
                self._rotate()
            cursor = step_end
        # Every baseline starts from a low quantile of its learned minutes, so a
        # log flood or an incident inside the learning window doesn't set what
        # "normal" means (and hide the next incident).
        for key, values in minutes.items():
            counters[key].baseline.mean = float(sorted(values)[len(values) // 4])
        self.cursor = end
        self.learning = False
        log.info(
            "Learned %d templates and %d exception fingerprints from %d lines",
            len(self.templates), len(self.exceptions), self.lines_seen,
        )

    async def run(self) -> None:
        if not await self.restore():
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
            if (now - self._last_snapshot).total_seconds() >= SNAPSHOT_EVERY_S:
                await self.save()
                self._last_snapshot = now

    def stats(self) -> dict:
        return {
            "lines_seen": self.lines_seen,
            "lines_parsed": self.lines_parsed,
            "lines_dropped": self.lines_dropped,
            "lag_s": round((utcnow() - self.cursor).total_seconds(), 1) if self.cursor else None,
            "templates": len(self.templates),
            "exception_fingerprints": len(self.exceptions),
            "restored_from_snapshot": self.restored,
        }

    # ------------------------------------------------------------ snapshots
    def snapshot(self) -> dict:
        def counter(c: Counter) -> dict:
            b = c.baseline
            return {"buckets": list(c.buckets), "total": c.total, "mean": b.mean, "var": b.var, "samples": b.samples}

        return {
            "version": 1,
            "cursor": self.cursor.isoformat() if self.cursor else None,
            "lines_seen": self.lines_seen,
            "lines_parsed": self.lines_parsed,
            "lines_dropped": self.lines_dropped,
            "miners": {s: jsonpickle.dumps(m.drain, keys=True) for s, m in self.miners.items()},
            "volume": {s: counter(c) for s, c in self.volume.items()},
            "templates": [
                {
                    "id": t.id, "service": t.service, "text": t.text, "level": t.level,
                    "first_seen": t.first_seen.isoformat(), "learned": t.learned, "counter": counter(t.counter),
                    "examples": list(t.examples), "history": list(t.history),
                }
                for t in self.templates.values()
            ],
            "exceptions": [
                {
                    "fingerprint": e.fingerprint, "service": e.service, "type": e.type, "message": e.message,
                    "frames": e.frames, "stack": e.stack, "version": e.version, "first_seen": e.first_seen.isoformat(),
                    "learned": e.learned, "counter": counter(e.counter), "trace_ids": list(e.trace_ids),
                }
                for e in self.exceptions.values()
            ],
        }

    def load(self, state: dict) -> None:
        def counter(data: dict) -> Counter:
            c = Counter()
            c.buckets.extend(data["buckets"])
            c.total = data["total"]
            c.baseline.mean, c.baseline.var, c.baseline.samples = data["mean"], data["var"], data["samples"]
            return c

        for service, encoded in state["miners"].items():
            if service in self.miners:
                drain = jsonpickle.loads(encoded, keys=True)
                miner = self.miners[service].drain
                miner.id_to_cluster = {int(k): v for k, v in drain.id_to_cluster.items()}
                miner.clusters_counter = drain.clusters_counter
                miner.root_node = drain.root_node
        for service, data in state["volume"].items():
            if service in self.volume:
                self.volume[service] = counter(data)
        for t in state["templates"]:
            self.templates[t["id"]] = Template(
                t["id"], t["service"], t["text"], t["level"], datetime.fromisoformat(t["first_seen"]), t["learned"],
                counter(t["counter"]), deque(t["examples"], maxlen=3), deque(map(tuple, t["history"]), maxlen=60),
            )
        for e in state["exceptions"]:
            self.exceptions[e["fingerprint"]] = Exception_(
                e["fingerprint"], e["service"], e["type"], e["message"], e["frames"], e["stack"], e["version"],
                datetime.fromisoformat(e["first_seen"]), e["learned"], counter(e["counter"]),
                deque(e["trace_ids"], maxlen=5),
            )
        self.lines_seen, self.lines_parsed = state["lines_seen"], state["lines_parsed"]
        self.lines_dropped = state.get("lines_dropped", 0)
        self.cursor = datetime.fromisoformat(state["cursor"]) if state["cursor"] else None

    async def save(self) -> None:
        try:
            await db.put_setting(SNAPSHOT_KEY, self.snapshot())
        except Exception as exc:  # noqa: BLE001 - a missed snapshot only costs a longer catch-up
            log.warning("Could not snapshot the log detector: %s", exc)

    async def restore(self) -> bool:
        """Resume from the last snapshot if it is recent enough to catch up from."""
        try:
            state = await db.get_setting(SNAPSHOT_KEY)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read the log detector snapshot: %s", exc)
            return False
        if not state or not state.get("cursor"):
            return False
        gap = utcnow() - datetime.fromisoformat(state["cursor"])
        if gap > timedelta(seconds=settings.learning_s):
            log.info("Snapshot is %s old; relearning instead", gap)
            return False
        self.load(state)
        self.restored = True
        self.learning = False
        self._last_rotate = self._last_minute = utcnow()
        log.info(
            "Resumed from snapshot (%d templates, %d fingerprints); catching up %.0f s of logs",
            len(self.templates), len(self.exceptions), gap.total_seconds(),
        )
        return True

    # ------------------------------------------------------------ queries
    def suspicious_in(self, services: set[str], since: datetime) -> dict | None:
        """The latest instruction-like WARN+ line from these services since `since`."""
        return next(
            (s for s in reversed(self.suspicious) if s["service"] in services and s["ts"] >= since), None
        )

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
