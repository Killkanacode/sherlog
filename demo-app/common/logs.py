"""Structured logging.

Every record goes to two places:
- stdout as one JSON object per line (what `docker logs` shows), and
- the OTel collector via OTLP, which forwards it to Loki.

Both carry service, version, level and the active trace/span ids. Exceptions
are logged with their full stack trace.
"""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider

_RESERVED_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI-colored duplicate of the message
}

_SEVERITY = {
    logging.DEBUG: SeverityNumber.DEBUG,
    logging.INFO: SeverityNumber.INFO,
    logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR,
    logging.CRITICAL: SeverityNumber.FATAL,
}


def _extra_fields(record: logging.LogRecord) -> dict:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_ATTRS and not key.startswith("_")
    }


def _exception_fields(record: logging.LogRecord) -> dict | None:
    if not record.exc_info or record.exc_info[0] is None:
        return None
    exc_type, exc_value, exc_tb = record.exc_info
    return {
        "type": exc_type.__name__,
        "message": str(exc_value),
        "stacktrace": "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    }


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str) -> None:
        super().__init__()
        self._service = service
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        doc = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "version": self._version,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": format(span_context.trace_id, "032x") if span_context.is_valid else None,
            "span_id": format(span_context.span_id, "016x") if span_context.is_valid else None,
        }
        doc.update(_extra_fields(record))
        exception = _exception_fields(record)
        if exception:
            doc["exception"] = exception
        return json.dumps(doc, default=str)


class OtlpLogHandler(logging.Handler):
    """Bridges stdlib logging to the OTel Logs API."""

    def __init__(self, provider: LoggerProvider) -> None:
        super().__init__()
        self._provider = provider

    def emit(self, record: logging.LogRecord) -> None:
        # Export failures are logged by the SDK itself; don't feed them back into it.
        if record.name.startswith("opentelemetry"):
            return
        try:
            attributes: dict = {"logger": record.name}
            for key, value in _extra_fields(record).items():
                attributes[key] = value if isinstance(value, (str, bool, int, float)) else str(value)
            exception = _exception_fields(record)
            if exception:
                attributes["exception.type"] = exception["type"]
                attributes["exception.message"] = exception["message"]
                attributes["exception.stacktrace"] = exception["stacktrace"]
            self._provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                context=otel_context.get_current(),
                severity_number=_SEVERITY.get(record.levelno, SeverityNumber.UNSPECIFIED),
                severity_text=record.levelname,
                body=record.getMessage(),
                attributes=attributes,
            )
        except Exception:
            self.handleError(record)


def configure_logging(service: str, version: str, provider: LoggerProvider | None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(JsonFormatter(service, version))
    root.addHandler(stdout)
    if provider is not None:
        root.addHandler(OtlpLogHandler(provider))

    # Route uvicorn through the root logger so its lines are structured too.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Access lines come from common.middleware; uvicorn's own stay off.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    # Keep library chatter out of the stream.
    for name in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
