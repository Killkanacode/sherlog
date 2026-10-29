"""Payments: charges a card through the external provider, idempotently."""

import json
import logging

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from common.app import create_app
from common.config import env_str
from payments.provider import SimulatedProvider

log = logging.getLogger("payments")

IDEMPOTENCY_TTL_S = 24 * 3600

store = redis.Redis.from_url(
    env_str("REDIS_URL", "redis://redis:6379/0"),
    socket_timeout=0.5,
    socket_connect_timeout=0.5,
)
provider = SimulatedProvider()


class ChargeRequest(BaseModel):
    order_ref: str
    amount_cents: int = Field(gt=0)
    currency: str = "USD"
    idempotency_key: str


async def lifespan(app: FastAPI):
    yield
    await store.aclose()


async def check_store() -> None:
    await store.ping()


app = create_app(lifespan=lifespan, health_checks={"redis": check_store})


@app.post("/charge")
async def charge(req: ChargeRequest):
    key = f"idem:charge:{req.idempotency_key}"
    try:
        previous = await store.get(key)
    except RedisError:
        log.exception("Idempotency store unavailable, refusing to charge order %s", req.order_ref)
        raise HTTPException(status_code=503, detail="idempotency store unavailable")
    if previous:
        return json.loads(previous)

    result = await provider.authorize(req.amount_cents, req.currency)
    authorization = result.get("authorization")
    if authorization is None:
        status, auth_code = "pending", None
        log.info(
            "Charge %s for order %s is pending provider review",
            result["id"],
            req.order_ref,
            extra={"charge_id": result["id"], "order_ref": req.order_ref},
        )
    else:
        status, auth_code = "approved", authorization["code"]

    response = {
        "charge_id": result["id"],
        "status": status,
        "auth_code": auth_code,
        "amount_cents": req.amount_cents,
        "currency": req.currency,
    }
    try:
        await store.set(key, json.dumps(response), ex=IDEMPOTENCY_TTL_S)
    except RedisError:
        log.exception("Failed to persist idempotency record for order %s", req.order_ref)
        raise HTTPException(status_code=503, detail="idempotency store unavailable")
    return response
