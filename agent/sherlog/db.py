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
CREATE TABLE IF NOT EXISTS sherlog.memory (
    incident_id  integer     PRIMARY KEY REFERENCES sherlog.incidents (id),
    closed_at    timestamptz NOT NULL,
    title        text        NOT NULL,
    service      text,
    category     text,
    summary      text,
    outcome      text        NOT NULL,
    action       jsonb,
    fingerprints text[]      NOT NULL,
    templates    text[]      NOT NULL,
    resolve_s    real
);
CREATE INDEX IF NOT EXISTS memory_fingerprints_idx ON sherlog.memory USING gin (fingerprints);
CREATE INDEX IF NOT EXISTS memory_templates_idx ON sherlog.memory USING gin (templates);
CREATE TABLE IF NOT EXISTS sherlog.settings (
    key   text  PRIMARY KEY,
    value jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS sherlog.audit (
    id      bigserial   PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    actor   text,
    role    text,
    method  text        NOT NULL,
    path    text        NOT NULL,
    status  integer     NOT NULL,
    ip      text
);
CREATE INDEX IF NOT EXISTS audit_ts_idx ON sherlog.audit (ts);
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


async def get_incident(incident_id: int) -> dict | None:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM sherlog.incidents WHERE id = :id"), {"id": incident_id})).first()
        return dict(row._mapping) if row else None


async def postmortems(limit: int = 50) -> list[dict]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, status, title, services, created_at, resolved_at FROM sherlog.incidents "
                "WHERE data ->> 'postmortem' IS NOT NULL ORDER BY id DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [dict(row._mapping) for row in rows]


# ------------------------------------------------------------ incident memory
MEMORY_COLUMNS = (
    "incident_id", "closed_at", "title", "service", "category", "summary", "outcome", "action",
    "fingerprints", "templates", "resolve_s",
)


async def remember(record: dict) -> None:
    columns = ", ".join(MEMORY_COLUMNS)
    values = ", ".join("CAST(:action AS jsonb)" if c == "action" else f":{c}" for c in MEMORY_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in MEMORY_COLUMNS[1:])
    async with engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO sherlog.memory ({columns}) VALUES ({values}) ON CONFLICT (incident_id) DO UPDATE SET {updates}"),
            {**record, "action": json.dumps(record["action"]) if record["action"] else None},
        )


async def recall(
    exclude_id: int, fingerprints: list[str] = (), templates: list[str] = (), service: str | None = None,
    template_like: str | None = None, limit: int = 5,
) -> list[dict]:
    """Past incidents sharing an exception fingerprint or a log template (or on
    a service), best matches first: a shared fingerprint outranks a template."""
    clauses, params = [], {"exclude": exclude_id, "limit": limit, "fps": list(fingerprints), "tpls": list(templates)}
    if fingerprints:
        clauses.append("fingerprints && CAST(:fps AS text[])")
    if templates:
        clauses.append("templates && CAST(:tpls AS text[])")
    if template_like:
        clauses.append("EXISTS (SELECT 1 FROM unnest(templates) t WHERE strpos(t, :pattern) > 0)")
        params["pattern"] = template_like
    if service:
        clauses.append("service = :service")
        params["service"] = service
    if not clauses:
        return []
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT *, "
                "cardinality(ARRAY(SELECT unnest(fingerprints) INTERSECT SELECT unnest(CAST(:fps AS text[])))) AS shared_fingerprints, "
                "cardinality(ARRAY(SELECT unnest(templates) INTERSECT SELECT unnest(CAST(:tpls AS text[])))) AS shared_templates "
                f"FROM sherlog.memory WHERE incident_id <> :exclude AND ({' OR '.join(clauses)}) "
                "ORDER BY shared_fingerprints DESC, shared_templates DESC, incident_id DESC LIMIT :limit"
            ),
            params,
        )
        return [dict(row._mapping) for row in rows]


async def key_frequencies(fingerprints: list[str], templates: list[str]) -> tuple[int, dict, dict]:
    """How many remembered incidents contain each key, and how many there are."""
    async with engine.connect() as conn:
        total = (await conn.execute(text("SELECT count(*) FROM sherlog.memory"))).scalar_one()
        counts = []
        for column, values in (("fingerprints", fingerprints), ("templates", templates)):
            rows = await conn.execute(
                text(
                    f"SELECT k, count(*) AS n FROM sherlog.memory, unnest({column}) AS k "
                    "WHERE k = ANY(CAST(:keys AS text[])) GROUP BY k"
                ),
                {"keys": list(values)},
            )
            counts.append({row.k: row.n for row in rows})
    return total, counts[0], counts[1]


async def month_usage() -> dict:
    """Model tokens and cost since the start of the month (cost null if any call is unpriced)."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(input_tokens + output_tokens), 0) AS tokens, "
                    "CASE WHEN bool_and(cost_usd IS NOT NULL) THEN coalesce(sum(cost_usd), 0)::float END AS cost_usd "
                    "FROM sherlog.llm_calls WHERE ts >= date_trunc('month', now())"
                )
            )
        ).first()
        return {"tokens": int(row.tokens), "cost_usd": row.cost_usd if row.cost_usd is not None else 0.0}


# ------------------------------------------------------------ audit
async def insert_audit(actor: str | None, role: str | None, method: str, path: str, status: int, ip: str | None) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sherlog.audit (actor, role, method, path, status, ip) "
                "VALUES (:actor, :role, :method, :path, :status, :ip)"
            ),
            {"actor": actor, "role": role, "method": method, "path": path, "status": status, "ip": ip},
        )


async def recent_audit(limit: int = 200) -> list[dict]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT * FROM sherlog.audit ORDER BY id DESC LIMIT :limit"), {"limit": limit})
        return [dict(row._mapping) for row in rows]


# ------------------------------------------------------------ settings
async def get_setting(key: str) -> dict | None:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT value FROM sherlog.settings WHERE key = :key"), {"key": key})).first()
        return row.value if row else None


async def put_setting(key: str, value: dict) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sherlog.settings (key, value) VALUES (:key, CAST(:value AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"key": key, "value": json.dumps(value)},
        )
