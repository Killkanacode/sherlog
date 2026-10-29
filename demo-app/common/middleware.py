"""Per-request telemetry: RED metrics, structured access log and a last-resort
exception handler that logs the full stack trace and returns a JSON 500."""

import logging
import time

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode
from starlette.responses import JSONResponse

access_log = logging.getLogger("access")
error_log = logging.getLogger("errors")

_meter = metrics.get_meter("common.http.server")
_server_duration = _meter.create_histogram(
    "http.server.duration",
    unit="s",
    description="Duration of inbound HTTP requests",
)

_SKIP_PATHS = {"/health"}


class TelemetryMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message):
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            status_code = 500
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            error_log.exception(
                "Unhandled %s while handling %s %s",
                type(exc).__name__,
                scope["method"],
                scope["path"],
            )
            if not response_started:
                response = JSONResponse(
                    {"error": "internal_error", "detail": type(exc).__name__}, status_code=500
                )
                await response(scope, receive, send)
        finally:
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            method = scope["method"]
            _server_duration.record(
                elapsed,
                {"route": route_path, "method": method, "status_code": str(status_code)},
            )
            access_log.info(
                "%s %s %d %.1fms",
                method,
                route_path,
                status_code,
                elapsed * 1000,
                extra={
                    "http_method": method,
                    "http_route": route_path,
                    "http_status": status_code,
                    "duration_ms": round(elapsed * 1000, 1),
                },
            )
