"""Metric anomaly detection over RED, saturation and dependency metrics.

Every tick each rule's PromQL is evaluated; each resulting series keeps an
EWMA baseline. A value is anomalous when it is far from the baseline in
z-score terms *and* in absolute terms (so a quiet series can't alert on noise).
A signal fires after `persistence` consecutive anomalous ticks and clears after
as many normal ones. Baselines freeze while a series is anomalous, and are
seeded from Prometheus history so Sherlog is warm right after it starts.
"""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sherlog.config import SERVICES, settings
from sherlog.detectors.baseline import Ewma
from sherlog.ingest.clients import prometheus
from sherlog.signals import bus, utcnow

log = logging.getLogger("sherlog.detectors.metrics")

ALPHA = 0.02  # ~8 min memory at a 10 s tick
MIN_SAMPLES = 30  # don't alert on a series with less than ~5 min of history


@dataclass(frozen=True)
class Rule:
    name: str
    query: str
    labels: tuple[str, ...]  # first label is the service; the rest qualify the series
    direction: str  # up | down | both
    min_delta: float  # absolute distance from baseline required
    min_ratio: float  # value >= mean * ratio (up) or <= mean / ratio (down)
    std_floor: float
    severity: str
    min_value: float = 0.0  # for "up": the value itself must reach this
    z: float = 4.0
    services_only: bool = True  # drop series whose service isn't a shop service
    impact: bool = True  # False for symptoms that usually mean "a caller changed"


W = "1m"
RULES = (
    Rule(
        "error_rate",
        f'(sum by (job) (rate(http_server_duration_seconds_count{{status_code=~"5.."}}[{W}])) '
        f"or sum by (job) (rate(http_server_duration_seconds_count[{W}])) * 0) "
        f"/ sum by (job) (rate(http_server_duration_seconds_count[{W}]))",
        ("job",), "up", min_delta=0.05, min_ratio=1.0, std_floor=0.01, severity="critical",
    ),
    Rule(
        "latency_p95",
        f"histogram_quantile(0.95, sum by (job, le) (rate(http_server_duration_seconds_bucket[{W}])))",
        ("job",), "up", min_delta=0.15, min_ratio=1.8, std_floor=0.02, severity="warning",
    ),
    Rule(
        "traffic",
        f"sum by (job) (rate(http_server_duration_seconds_count[{W}]))",
        ("job",), "both", min_delta=0.5, min_ratio=2.5, std_floor=0.2, severity="warning", impact=False,
    ),
    Rule(
        "client_failure_rate",
        f'(sum by (job, peer) (rate(http_client_duration_seconds_count{{outcome!="success"}}[{W}])) '
        f"or sum by (job, peer) (rate(http_client_duration_seconds_count[{W}])) * 0) "
        f"/ sum by (job, peer) (rate(http_client_duration_seconds_count[{W}]))",
        ("job", "peer"), "up", min_delta=0.05, min_ratio=1.0, std_floor=0.01, severity="critical",
    ),
    Rule(
        "client_latency_p95",
        f"histogram_quantile(0.95, sum by (job, peer, le) (rate(http_client_duration_seconds_bucket[{W}])))",
        ("job", "peer"), "up", min_delta=0.25, min_ratio=2.0, std_floor=0.02, severity="warning",
    ),
    Rule(
        "cpu_cores",
        f"sum by (job) (rate(process_cpu_time_seconds_total[{W}]))",
        ("job",), "up", min_delta=0.3, min_ratio=3.0, std_floor=0.02, severity="warning",
    ),
    Rule(
        "memory_growth_bytes_per_s",
        "deriv(max by (job) (process_memory_rss_bytes)[2m:10s])",
        ("job",), "up", min_delta=150_000, min_ratio=1.0, std_floor=20_000, severity="warning",
    ),
    Rule(
        "memory_used_ratio",
        "max by (job) (process_memory_rss_bytes) / max by (job) (process_memory_limit_bytes)",
        ("job",), "up", min_delta=0.15, min_ratio=1.0, std_floor=0.02, severity="critical", min_value=0.8,
    ),
    Rule(
        "db_pool_saturation",
        "max by (job) (db_pool_connections_in_use) / max by (job) (db_pool_connections_max)",
        ("job",), "up", min_delta=0.5, min_ratio=1.0, std_floor=0.05, severity="critical", min_value=0.9,
    ),
    Rule(
        "dependency_failure_rate",
        f"sum by (server) (rate(traces_service_graph_request_failed_total[{W}])) "
        f"/ sum by (server) (rate(traces_service_graph_request_total[{W}]))",
        ("server",), "up", min_delta=0.05, min_ratio=1.0, std_floor=0.01, severity="critical", services_only=False,
    ),
)


@dataclass
class SeriesState:
    baseline: Ewma = field(default_factory=lambda: Ewma(ALPHA))
    anomalous_ticks: int = 0
    normal_ticks: int = 0
    first_anomalous_at: datetime | None = None
    firing: bool = False
    last_value: float | None = None
    last_z: float = 0.0


class MetricDetector:
    def __init__(self) -> None:
        self.series: dict[tuple, SeriesState] = {}

    @staticmethod
    def _key(rule: Rule, metric: dict) -> tuple | None:
        values = tuple(metric.get(label) for label in rule.labels)
        if values[0] is None:
            return None
        if rule.services_only and values[0] not in SERVICES:
            return None
        if not rule.services_only and values[0] in SERVICES + ("user",):
            return None  # dependency rule covers datastores and externals only
        return (rule.name, *values)

    async def seed(self) -> None:
        end = utcnow()
        start = end - timedelta(seconds=settings.learning_s)
        for rule in RULES:
            try:
                result = await prometheus.query_range(rule.query, start, end, settings.detect_interval_s)
            except Exception as exc:  # noqa: BLE001 - seeding is best effort
                log.warning("Could not seed %s: %s", rule.name, exc)
                continue
            for series in result:
                key = self._key(rule, series["metric"])
                if key is None:
                    continue
                state = self.series.setdefault(key, SeriesState())
                for _, raw in series["values"]:
                    value = float(raw)
                    if math.isfinite(value):
                        state.baseline.update(value)
        log.info("Seeded %d metric series from %ds of history", len(self.series), settings.learning_s)

    def _evaluate(self, rule: Rule, state: SeriesState, value: float) -> tuple[bool, float, str]:
        base = state.baseline
        if base.mean is None or base.samples < MIN_SAMPLES:
            return False, 0.0, ""
        mean = base.mean
        std = max(base.std, rule.std_floor, 0.1 * abs(mean))
        z = (value - mean) / std
        up = (
            rule.direction in ("up", "both")
            and value - mean >= rule.min_delta
            and value >= mean * rule.min_ratio
            and value >= rule.min_value
            and z >= rule.z
        )
        down = (
            rule.direction in ("down", "both")
            and mean - value >= rule.min_delta
            and value <= mean / rule.min_ratio
            and z <= -rule.z
        )
        return up or down, z, "up" if up else "down" if down else ""

    async def tick(self) -> None:
        now = utcnow()
        for rule in RULES:
            try:
                result = await prometheus.query(rule.query)
            except Exception as exc:  # noqa: BLE001
                log.warning("Query for %s failed: %s", rule.name, exc)
                continue
            seen = set()
            for series in result:
                key = self._key(rule, series["metric"])
                if key is None:
                    continue
                value = float(series["value"][1])
                if not math.isfinite(value):
                    continue
                seen.add(key)
                self._observe(rule, key, value, now)
            # A traffic series that vanished entirely means zero traffic.
            if rule.name == "traffic":
                for key in [k for k in self.series if k[0] == "traffic" and k not in seen]:
                    self._observe(rule, key, 0.0, now)

    def _observe(self, rule: Rule, key: tuple, value: float, now: datetime) -> None:
        state = self.series.setdefault(key, SeriesState())
        anomalous, z, direction = self._evaluate(rule, state, value)
        state.last_value, state.last_z = value, z
        service = key[1]
        signal_key = "metric:" + ":".join(str(part) for part in key)
        if anomalous:
            state.anomalous_ticks += 1
            state.normal_ticks = 0
            state.first_anomalous_at = state.first_anomalous_at or now
            if state.anomalous_ticks >= settings.persistence:
                state.firing = True
                details = {
                    "metric": rule.name,
                    "direction": direction,
                    "value": round(value, 4),
                    "baseline": round(state.baseline.mean, 4),
                    "z": round(z, 1),
                    "impact": rule.impact,
                }
                if len(key) > 2:
                    details["peer"] = key[2]
                bus.fire(signal_key, "metric_anomaly", service, rule.severity, details, state.first_anomalous_at)
            return

        state.anomalous_ticks = 0
        state.first_anomalous_at = None if not state.firing else state.first_anomalous_at
        if state.firing:
            state.normal_ticks += 1
            if state.normal_ticks >= settings.persistence:
                state.firing = False
                state.first_anomalous_at = None
                bus.clear(signal_key)
            return
        state.baseline.update(value)

    async def run(self) -> None:
        await self.seed()
        while True:
            started = asyncio.get_running_loop().time()
            await self.tick()
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, settings.detect_interval_s - elapsed))

    def snapshot(self) -> list[dict]:
        return [
            {
                "rule": key[0],
                "service": key[1],
                "qualifier": key[2] if len(key) > 2 else None,
                "value": state.last_value,
                "baseline": state.baseline.mean,
                "z": round(state.last_z, 2),
                "firing": state.firing,
            }
            for key, state in sorted(self.series.items())
        ]


metric_detector = MetricDetector()
