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
CREATE TABLE IF NOT EXISTS sherlog.llm_calls (
    id            bigserial   PRIMARY KEY,
    incident_id   integer     REFERENCES sherlog.incidents (id),
    ts            timestamptz NOT NULL,
    role          text        NOT NULL,
    model         text        NOT NULL,
    input_tokens  integer     NOT NULL,
    output_tokens integer     NOT NULL,
    latency_ms    real        NOT NULL,
    cost_usd      numeric
);
CREATE INDEX IF NOT EXISTS llm_calls_incident_idx ON sherlog.llm_calls (incident_id);
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


async def insert_llm_call(incident_id: int | None, call: dict) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sherlog.llm_calls "
                "(incident_id, ts, role, model, input_tokens, output_tokens, latency_ms, cost_usd) "
                "VALUES (:incident_id, :ts, :role, :model, :input_tokens, :output_tokens, :latency_ms, :cost_usd)"
            ),
            {**call, "incident_id": incident_id, "ts": datetime.fromisoformat(call["ts"])},
        )


async def llm_totals() -> list[dict]:
    """Calls, tokens and cost per model since the beginning (cost is null if unpriced)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT model, count(*) AS calls, sum(input_tokens) AS input_tokens, "
                "sum(output_tokens) AS output_tokens, "
                "CASE WHEN bool_and(cost_usd IS NOT NULL) THEN sum(cost_usd)::float END AS cost_usd "
                "FROM sherlog.llm_calls GROUP BY model ORDER BY model"
            )
        )
        return [dict(row._mapping) for row in rows]


async def search_incidents(
    exclude_id: int, service: str | None = None, needles: list[str] | None = None, limit: int = 5
) -> list[dict]:
    """Earlier incidents on a service, or whose stored data mentions any needle
    (an exception fingerprint or a log template)."""
    clauses, params = [], {"exclude": exclude_id, "limit": limit}
    if service:
        clauses.append(":service = ANY(services)")
        params["service"] = service
    for i, needle in enumerate(needles or []):
        clauses.append(f"strpos(data::text, :needle{i}) > 0")
        params[f"needle{i}"] = needle
    if not clauses:
        return []
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, status, title, services, created_at, resolved_at, "
                "data -> 'diagnosis' AS diagnosis FROM sherlog.incidents "
                f"WHERE id <> :exclude AND ({' OR '.join(clauses)}) ORDER BY id DESC LIMIT :limit"
            ),
            params,
        )
        return [dict(row._mapping) for row in rows]
