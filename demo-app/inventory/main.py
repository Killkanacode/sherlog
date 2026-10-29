"""Inventory: product catalog and stock reservations (Postgres + Redis cache)."""

import json
import logging
import uuid

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from redis.exceptions import RedisError
from sqlalchemy import text

from common.app import create_app
from common.config import env_int, env_str
from common.db import create_engine, run_migrations

log = logging.getLogger("inventory")

CACHE_TTL_S = env_int("CACHE_TTL_S", 30)

engine = create_engine()
cache = redis.Redis.from_url(
    env_str("REDIS_URL", "redis://redis:6379/0"),
    socket_timeout=0.5,
    socket_connect_timeout=0.5,
)


class ReserveItem(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=100)


class ReserveRequest(BaseModel):
    order_ref: str
    items: list[ReserveItem] = Field(min_length=1, max_length=20)


async def cache_get(key: str):
    try:
        raw = await cache.get(key)
    except RedisError as exc:
        log.warning("Cache read failed for %s: %s", key, exc, extra={"cache_key": key})
        return None
    return json.loads(raw) if raw else None


async def cache_set(key: str, value) -> None:
    try:
        await cache.set(key, json.dumps(value), ex=CACHE_TTL_S)
    except RedisError as exc:
        log.warning("Cache write failed for %s: %s", key, exc, extra={"cache_key": key})


async def lifespan(app: FastAPI):
    await run_migrations(engine, "inventory")
    yield
    await cache.aclose()
    await engine.dispose()


async def check_db() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


app = create_app(lifespan=lifespan, health_checks={"postgres": check_db})


@app.get("/products")
async def list_products(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
    key = f"products:list:{limit}:{offset}"
    cached = await cache_get(key)
    if cached is not None:
        return cached
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT sku, name, unit_price_cents, stock FROM products "
                "ORDER BY id LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        products = [dict(row._mapping) for row in rows]
    body = {"items": products, "limit": limit, "offset": offset}
    await cache_set(key, body)
    return body


@app.get("/products/{sku}")
async def get_product(sku: str):
    key = f"products:sku:{sku}"
    cached = await cache_get(key)
    if cached is not None:
        return cached
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT sku, name, description, unit_price_cents, stock "
                    "FROM products WHERE sku = :sku"
                ),
                {"sku": sku},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
    product = dict(row._mapping)
    await cache_set(key, product)
    return product


@app.post("/reserve")
async def reserve(req: ReserveRequest):
    reserved = []
    async with engine.begin() as conn:
        for item in req.items:
            row = (
                await conn.execute(
                    text(
                        "UPDATE products SET stock = stock - :qty, updated_at = now() "
                        "WHERE sku = :sku AND stock >= :qty "
                        "RETURNING sku, name, unit_price_cents"
                    ),
                    {"sku": item.sku, "qty": item.qty},
                )
            ).first()
            if row is None:
                log.info(
                    "Reservation rejected for %s: unknown sku or insufficient stock",
                    item.sku,
                    extra={"sku": item.sku, "order_ref": req.order_ref},
                )
                raise HTTPException(status_code=409, detail=f"cannot reserve {item.sku}")
            reserved.append(
                {"sku": row.sku, "name": row.name, "qty": item.qty, "unit_price": row.unit_price_cents}
            )
    return {"reservation_id": str(uuid.uuid4()), "order_ref": req.order_ref, "items": reserved}
