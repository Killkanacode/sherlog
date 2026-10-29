"""Gateway: the public edge of the shop. Routes requests to internal services."""

import logging

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from common.app import create_app
from common.config import env_float, env_str
from common.http import Downstream, DownstreamError

log = logging.getLogger("gateway")

inventory = Downstream(
    "inventory", env_str("INVENTORY_URL", "http://inventory:8003"), env_float("INVENTORY_TIMEOUT_S", 3.0)
)
# Checkout is not idempotent at the edge, so the gateway never retries it.
orders = Downstream("orders", env_str("ORDERS_URL", "http://orders:8001"), env_float("ORDERS_TIMEOUT_S", 8.0), retries=0)


async def lifespan(app: FastAPI):
    yield
    await inventory.close()
    await orders.close()


app = create_app(lifespan=lifespan)


async def forward(downstream: Downstream, method: str, path: str, **kwargs) -> JSONResponse:
    try:
        response = await downstream.request(method, path, **kwargs)
    except DownstreamError as exc:
        status = 504 if exc.kind == "timeout" else 503 if exc.kind == "connect_error" else 502
        log.error(
            "Upstream %s failed for %s %s: %s",
            exc.peer,
            method,
            path,
            exc,
            extra={"peer": exc.peer, "error_kind": exc.kind, "peer_status": exc.status_code},
        )
        return JSONResponse({"error": "upstream_unavailable", "upstream": exc.peer}, status_code=status)
    return JSONResponse(response.json(), status_code=response.status_code)


@app.get("/products")
async def list_products(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await forward(inventory, "GET", "/products", params={"limit": limit, "offset": offset})


@app.get("/products/{sku}")
async def get_product(sku: str):
    return await forward(inventory, "GET", f"/products/{sku}")


@app.post("/checkout")
async def checkout(request: Request):
    return await forward(orders, "POST", "/orders", json=await request.json())


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    return await forward(orders, "GET", f"/orders/{order_id}")
