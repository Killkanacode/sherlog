"""Guardrails around every model client: redaction, a monthly budget and a
circuit breaker.

  redaction  secrets and personal data are scrubbed from every message
  budget     calls stop once the month's spend or tokens reach the limit
  breaker    after N consecutive failures, calls pause for a cooldown and
             incidents escalate straight away instead of waiting on timeouts
"""

import logging
import time

from sherlog import db
from sherlog.config import settings
from sherlog.llm.base import ChatModel, Completion, LLMError
from sherlog.llm.redact import redact_message

log = logging.getLogger("sherlog.llm")

BUDGET_CACHE_S = 15


class Breaker:
    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def check(self) -> None:
        if time.monotonic() < self.open_until:
            left = round(self.open_until - time.monotonic())
            raise LLMError(f"model calls paused for {left} s after {self.failures} consecutive failures")

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= settings.breaker_failures:
            self.open_until = time.monotonic() + settings.breaker_cooldown_s
            log.warning("Model breaker open for %d s after %d failures", settings.breaker_cooldown_s, self.failures)

    def state(self) -> dict:
        return {"open": time.monotonic() < self.open_until, "consecutive_failures": self.failures}


class Budget:
    def __init__(self) -> None:
        self._usage: dict | None = None
        self._read_at = 0.0

    async def usage(self) -> dict:
        if self._usage is None or time.monotonic() - self._read_at > BUDGET_CACHE_S:
            self._usage = await db.month_usage()
            self._read_at = time.monotonic()
        return self._usage

    def spent(self, completion: Completion, cost: float | None) -> None:
        if self._usage is not None:
            self._usage = {
                "tokens": self._usage["tokens"] + completion.input_tokens + completion.output_tokens,
                "cost_usd": self._usage["cost_usd"] + (cost or 0.0),
            }

    async def check(self) -> None:
        if not (settings.monthly_spend_limit_usd or settings.monthly_token_limit):
            return
        usage = await self.usage()
        if settings.monthly_spend_limit_usd and usage["cost_usd"] >= settings.monthly_spend_limit_usd:
            raise LLMError(f"monthly model budget of ${settings.monthly_spend_limit_usd:g} reached")
        if settings.monthly_token_limit and usage["tokens"] >= settings.monthly_token_limit:
            raise LLMError(f"monthly limit of {settings.monthly_token_limit:,} model tokens reached")

    async def state(self) -> dict:
        try:
            usage = await self.usage()
        except Exception:  # noqa: BLE001 - status only
            usage = None
        return {
            "month": usage,
            "spend_limit_usd": settings.monthly_spend_limit_usd or None,
            "token_limit": settings.monthly_token_limit or None,
        }


breaker = Breaker()
budget = Budget()


class GuardedModel:
    def __init__(self, inner: ChatModel) -> None:
        self.inner = inner
        self.model = inner.model
        self.price_in, self.price_out = inner.price_in, inner.price_out

    async def complete(self, messages: list[dict], **kwargs) -> Completion:
        breaker.check()
        await budget.check()
        try:
            completion = await self.inner.complete([redact_message(m) for m in messages], **kwargs)
        except LLMError:
            breaker.failure()
            raise
        breaker.success()
        cost = None
        if self.price_in is not None and self.price_out is not None:
            cost = (completion.input_tokens * self.price_in + completion.output_tokens * self.price_out) / 1_000_000
        budget.spent(completion, cost)
        return completion
