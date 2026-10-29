"""Sherlog's own Postgres (schema `sherlog`): incidents and change events."""

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sherlog.config import settings

engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=5, pool_pre_ping=True)

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS sherlog;
CREATE TABLE IF NOT EXISTS sherlog.incidents (
    id          serial      PRIMARY KEY,
    status      text        NOT NULL,
    title       text        NOT NULL,
    services    text[]      NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL,
    resolved_at timestamptz,
    data        jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_status_idx ON sherlog.incidents (status);
CREATE TABLE IF NOT EXISTS sherlog.change_events (
    id      text        PRIMARY KEY,
    ts      timestamptz NOT NULL,
    type    text        NOT NULL,
    service text,
    data    jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS change_events_ts_idx ON sherlog.change_events (ts);
"""


async def init() -> None:
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        await raw.driver_connection.execute(SCHEMA)


async def insert_incident(status: str, title: str, services: list[str], created_at: datetime, data: dict) -> int:
    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "INSERT INTO sherlog.incidents (status, title, services, created_at, updated_at, data) "
                "VALUES (:status, :title, :services, :created_at, :created_at, CAST(:data AS jsonb)) RETURNING id"
            ),
            {"status": status, "title": title, "services": services, "created_at": created_at, "data": json.dumps(data)},
        )
        return row.scalar_one()


async def update_incident(
    incident_id: int, status: str, title: str, services: list[str], updated_at: datetime,
    resolved_at: datetime | None, data: dict,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sherlog.incidents SET status = :status, title = :title, services = :services, "
                "updated_at = :updated_at, resolved_at = :resolved_at, data = CAST(:data AS jsonb) WHERE id = :id"
            ),
            {
                "id": incident_id, "status": status, "title": title, "services": services,
                "updated_at": updated_at, "resolved_at": resolved_at, "data": json.dumps(data),
            },
        )


async def recent_incidents(limit: int = 100) -> list[dict]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT * FROM sherlog.incidents ORDER BY id DESC LIMIT :limit"), {"limit": limit}
        )
        return [dict(row._mapping) for row in rows]


async def insert_event(event: dict) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO sherlog.change_events (id, ts, type, service, data) "
                "VALUES (:id, :ts, :type, :service, CAST(:data AS jsonb)) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": event["id"],
                "ts": datetime.fromisoformat(event["timestamp"]),
                "type": event["type"],
                "service": event.get("service"),
                "data": json.dumps(event),
            },
        )
        return result.rowcount == 1


async def events_between(start: datetime, end: datetime) -> list[dict]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT data FROM sherlog.change_events WHERE ts BETWEEN :start AND :end ORDER BY ts"),
            {"start": start, "end": end},
        )
        return [row.data for row in rows]


async def recent_events(limit: int = 100) -> list[dict]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT data FROM sherlog.change_events ORDER BY ts DESC LIMIT :limit"), {"limit": limit}
        )
        return [row.data for row in rows][::-1]
