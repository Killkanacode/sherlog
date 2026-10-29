"""Postgres access: pooled async engine, pool gauges and startup migrations."""

import logging
from pathlib import Path

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from common.config import env_float, env_int, env_str
from common.telemetry import instrument_engine

log = logging.getLogger("db")

MIGRATIONS_ROOT = Path(__file__).resolve().parent.parent / "db"


def create_engine() -> AsyncEngine:
    pool_size = env_int("DB_POOL_SIZE", 10)
    engine = create_async_engine(
        env_str("DATABASE_URL"),
        pool_size=pool_size,
        max_overflow=env_int("DB_MAX_OVERFLOW", 0),
        pool_timeout=env_float("DB_POOL_TIMEOUT_S", 3.0),
        pool_pre_ping=True,
    )
    instrument_engine(engine)
    _register_pool_metrics(engine)
    log.info("Database pool configured (size=%d)", pool_size, extra={"db_pool_size": pool_size})
    return engine


def _register_pool_metrics(engine: AsyncEngine) -> None:
    meter = metrics.get_meter("common.db")
    pool = engine.sync_engine.pool

    def in_use(_: CallbackOptions):
        yield Observation(pool.checkedout())

    def size(_: CallbackOptions):
        yield Observation(pool.size())

    meter.create_observable_gauge(
        "db.pool.connections.in_use", callbacks=[in_use], description="Checked-out connections"
    )
    meter.create_observable_gauge(
        "db.pool.connections.max", callbacks=[size], description="Configured pool size"
    )


async def run_migrations(engine: AsyncEngine, component: str) -> None:
    """Apply db/<component>/*.sql in order.

    Migrations are written to be idempotent and are re-applied on every start,
    so the schema always matches the code version that is running.
    """
    files = sorted((MIGRATIONS_ROOT / component).glob("*.sql"))
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection  # asyncpg: allows multi-statement scripts
        await driver.execute("SELECT pg_advisory_lock(hashtext('migrations'))")
        try:
            for path in files:
                await driver.execute(path.read_text())
                log.info("Applied migration %s", path.name, extra={"migration": path.name})
        finally:
            await driver.execute("SELECT pg_advisory_unlock(hashtext('migrations'))")
