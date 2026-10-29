"""Simulated external card processor.

Latency is uniform in [PROVIDER_LATENCY_MS_MIN, PROVIDER_LATENCY_MS_MAX] (real
card processors take a few hundred ms) and concurrency is capped like a real
provider's rate limit. A fraction of charges come back pending manual review,
with no authorization attached.
"""

import asyncio
import random
import secrets
import uuid

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from common.config import env_float, env_int

tracer = trace.get_tracer("payments.provider")


class SimulatedProvider:
    def __init__(self) -> None:
        self.latency_ms = (
            env_int("PROVIDER_LATENCY_MS_MIN", 200),
            env_int("PROVIDER_LATENCY_MS_MAX", 500),
        )
        self.pending_rate = env_float("PROVIDER_PENDING_RATE", 0.1)
        self._slots = asyncio.Semaphore(env_int("PROVIDER_MAX_CONCURRENCY", 16))

    async def authorize(self, amount_cents: int, currency: str) -> dict:
        with tracer.start_as_current_span(
            "card-provider authorize",
            kind=SpanKind.CLIENT,
            attributes={"peer.service": "card-provider", "payment.amount_cents": amount_cents},
        ):
            async with self._slots:
                await asyncio.sleep(random.uniform(*self.latency_ms) / 1000)

        charge_id = f"ch_{uuid.uuid4().hex[:16]}"
        if random.random() < self.pending_rate:
            return {"id": charge_id, "status": "pending_review", "authorization": None}
        return {
            "id": charge_id,
            "status": "approved",
            "authorization": {
                "code": secrets.token_hex(3).upper(),
                "network": random.choice(["visa", "mastercard", "amex"]),
            },
        }
