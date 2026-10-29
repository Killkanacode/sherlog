"""Orders: reserves stock, charges the customer and records the order."""

import logging
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from common.app import create_app
from common.config import env_float, env_str
from common.db import create_engine, run_migrations
from common.flags import FlagClient
from common.http import Downstream, DownstreamError
from orders import pricing

log = logging.getLogger("orders")

engine = create_engine()
flags = FlagClient()
inventory = Downstream(
    "inventory", env_str("INVENTORY_URL", "http://inventory:8003"), env_float("INVENTORY_TIMEOUT_S", 2.0)
)
payments = Downstream(
    "payments", env_str("PAYMENTS_URL", "http://payments:8002"), env_float("PAYMENTS_TIMEOUT_S", 2.0)
)


class OrderItem(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=100)


class CreateOrder(BaseModel):
    user_id: str
    items: list[OrderItem] = Field(min_length=1, max_length=20)


async def lifespan(app: FastAPI):
    await run_migrations(engine, "orders")
    await flags.start()
    yield
    await flags.stop()
    await inventory.close()
    await payments.close()
    await engine.dispose()


async def check_db() -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


app = create_app(lifespan=lifespan, health_checks={"postgres": check_db})


def _dependency_failure(exc: DownstreamError, order_id: uuid.UUID) -> HTTPException:
    log.error(
        "Order %s failed: %s",
        order_id,
        exc,
        extra={"order_id": str(order_id), "peer": exc.peer, "error_kind": exc.kind},
    )
    status = 504 if exc.kind == "timeout" else 503 if exc.kind == "connect_error" else 502
    return HTTPException(status_code=status, detail=f"{exc.peer} {exc.kind}")


@app.post("/orders", status_code=201)
async def create_order(req: CreateOrder):
    order_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO orders (id, user_id, status) VALUES (:id, :user_id, 'pending')"),
            {"id": order_id, "user_id": req.user_id},
        )

        try:
            reservation = await inventory.request(
                "POST",
                "/reserve",
                json={"order_ref": str(order_id), "items": [i.model_dump() for i in req.items]},
            )
        except DownstreamError as exc:
            raise _dependency_failure(exc, order_id)
        if reservation.status_code == 409:
            raise HTTPException(status_code=409, detail=reservation.json().get("detail"))
        if reservation.status_code >= 400:
            log.error("Inventory rejected reservation for order %s: %s", order_id, reservation.text[:200])
            raise HTTPException(status_code=502, detail="inventory rejected reservation")
        lines = reservation.json()["items"]

        if flags.is_enabled("new_pricing"):
            total = pricing.order_total_volume_discount(lines)
        else:
            total = pricing.order_total(lines)

        try:
            charge = await payments.request(
                "POST",
                "/charge",
                json={
                    "order_ref": str(order_id),
                    "amount_cents": total,
                    "currency": "USD",
                    "idempotency_key": str(order_id),
                },
            )
        except DownstreamError as exc:
            raise _dependency_failure(exc, order_id)
        if charge.status_code >= 400:
            log.error(
                "Payment rejected for order %s with status %d: %s",
                order_id,
                charge.status_code,
                charge.text[:300],
                extra={"order_id": str(order_id), "peer": "payments", "peer_status": charge.status_code},
            )
            raise HTTPException(status_code=502, detail="payment rejected")
        payment = charge.json()

        status = "paid" if payment["status"] == "approved" else "payment_pending"
        await conn.execute(
            text(
                "INSERT INTO order_items (order_id, sku, qty, unit_price_cents) "
                "VALUES (:order_id, :sku, :qty, :unit_price)"
            ),
            [
                {"order_id": order_id, "sku": line["sku"], "qty": line["qty"], "unit_price": line["unit_price"]}
                for line in lines
            ],
        )
        await conn.execute(
            text(
                "UPDATE orders SET status = :status, total_cents = :total, charge_id = :charge_id, "
                "updated_at = now() WHERE id = :id"
            ),
            {"id": order_id, "status": status, "total": total, "charge_id": payment["charge_id"]},
        )

    return {"order_id": str(order_id), "status": status, "total_cents": total, "items": lines}


@app.get("/orders/{order_id}")
async def get_order(order_id: uuid.UUID):
    async with engine.connect() as conn:
        order = (
            await conn.execute(
                text("SELECT id, user_id, status, total_cents, created_at FROM orders WHERE id = :id"),
                {"id": order_id},
            )
        ).first()
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        items = await conn.execute(
            text("SELECT sku, qty, unit_price_cents FROM order_items WHERE order_id = :id"),
            {"id": order_id},
        )
        return {**dict(order._mapping), "items": [dict(row._mapping) for row in items]}
