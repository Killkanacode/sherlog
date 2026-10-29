"""FastAPI application factory shared by every demo service."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.config import SERVICE_NAME, SERVICE_VERSION
from common.telemetry import instrument_app, setup_telemetry, shutdown_telemetry

setup_telemetry()

from common.middleware import TelemetryMiddleware  # noqa: E402  (needs telemetry first)

log = logging.getLogger("app")

HealthCheck = Callable[[], Awaitable[None]]


def create_app(
    lifespan: Callable[[FastAPI], AsyncIterator[None]] | None = None,
    health_checks: dict[str, HealthCheck] | None = None,
) -> FastAPI:
    """Build a service app.

    lifespan: optional async generator for startup/shutdown of service resources.
    health_checks: named async callables; any exception marks /health as 503.
    """
    checks = health_checks or {}

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        log.info("Starting %s version %s", SERVICE_NAME, SERVICE_VERSION)
        if lifespan is not None:
            async with asynccontextmanager(lifespan)(app):
                yield
        else:
            yield
        log.info("Stopping %s", SERVICE_NAME)
        shutdown_telemetry()

    app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=_lifespan)
    app.add_middleware(TelemetryMiddleware)
    instrument_app(app)

    @app.get("/health")
    async def health():
        results, healthy = {}, True
        for name, check in checks.items():
            try:
                await check()
                results[name] = "ok"
            except Exception as exc:
                results[name] = f"failing: {type(exc).__name__}"
                healthy = False
        body = {
            "status": "ok" if healthy else "failing",
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "checks": results,
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    return app
