"""OpenTelemetry setup shared by every demo service.

Traces, metrics and logs are exported over OTLP/HTTP to the collector
(OTEL_EXPORTER_OTLP_ENDPOINT). Custom RED metrics are recorded by
`common.middleware` and `common.http`; auto-instrumentation is used for spans
only so metric names stay stable.
"""

import os
import socket

import psutil
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import CallbackOptions, NoOpMeterProvider, Observation
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from common.config import SERVICE_NAME, SERVICE_VERSION, env_int, env_str
from common.logs import configure_logging

# Seconds. Covers fast cache hits through multi-second timeouts.
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_providers: dict = {}


def setup_telemetry() -> None:
    """Configure global providers and logging. Safe to call more than once."""
    if _providers:
        return

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "service.instance.id": socket.gethostname(),
            "deployment.environment.name": env_str("DEPLOY_ENV", "demo"),
        }
    )
    exporting = bool(env_str("OTEL_EXPORTER_OTLP_ENDPOINT"))

    tracer_provider = TracerProvider(resource=resource)
    if exporting:
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    readers = []
    if exporting:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(),
                export_interval_millis=env_int("OTEL_METRIC_EXPORT_INTERVAL", 5000),
            )
        )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=readers,
        views=[
            View(
                instrument_name="http.*.duration",
                aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS),
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    if exporting:
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    configure_logging(SERVICE_NAME, SERVICE_VERSION, logger_provider)

    HTTPXClientInstrumentor().instrument(meter_provider=NoOpMeterProvider())
    RedisInstrumentor().instrument()
    _register_process_metrics()

    _providers.update(tracer=tracer_provider, meter=meter_provider, logger=logger_provider)


def instrument_app(app) -> None:
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="health",
        meter_provider=NoOpMeterProvider(),
        exclude_spans=["receive", "send"],
    )


def instrument_engine(engine) -> None:
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine,
        meter_provider=NoOpMeterProvider(),
        enable_commenter=False,
    )


def shutdown_telemetry() -> None:
    for key in ("tracer", "meter", "logger"):
        provider = _providers.get(key)
        if provider is not None:
            provider.shutdown()


def _memory_limit_bytes() -> int | None:
    # cgroup v2 (Docker Desktop and modern Linux hosts).
    try:
        with open("/sys/fs/cgroup/memory.max") as fh:
            raw = fh.read().strip()
        return None if raw == "max" else int(raw)
    except (OSError, ValueError):
        return None


def _register_process_metrics() -> None:
    meter = metrics.get_meter("common.process")
    process = psutil.Process(os.getpid())
    limit = _memory_limit_bytes()

    def rss(_: CallbackOptions):
        yield Observation(process.memory_info().rss)

    def cpu(_: CallbackOptions):
        times = process.cpu_times()
        yield Observation(times.user + times.system)

    def memory_limit(_: CallbackOptions):
        if limit is not None:
            yield Observation(limit)

    meter.create_observable_gauge(
        "process.memory.rss", callbacks=[rss], unit="By", description="Resident set size"
    )
    meter.create_observable_counter(
        "process.cpu.time", callbacks=[cpu], unit="s", description="CPU time (user + system)"
    )
    meter.create_observable_gauge(
        "process.memory.limit",
        callbacks=[memory_limit],
        unit="By",
        description="Container memory limit (cgroup)",
    )
