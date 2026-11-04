"""Per-incident accounting of model calls: tokens, latency and cost."""

from collections import defaultdict
from dataclasses import dataclass, field

from sherlog import db
from sherlog.llm.base import ChatModel, Completion, cost_usd
from sherlog.signals import utcnow


@dataclass
class Usage:
    incident_id: int
    calls: list[dict] = field(default_factory=list)

    async def record(self, role: str, model: ChatModel, completion: Completion) -> dict:
        return await self.add(
            role, completion.model or model.model, completion.input_tokens, completion.output_tokens,
            completion.latency_ms, cost_usd(model, completion),
        )

    async def add(
        self, role: str, model: str, input_tokens: int, output_tokens: int, latency_ms: float, cost: float | None
    ) -> dict:
        """Record model usage reported in bulk (the fixer's Muse Code run)."""
        call = {
            "ts": utcnow().isoformat(),
            "role": role,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "cost_usd": cost,
        }
        self.calls.append(call)
        await db.insert_llm_call(self.incident_id, call)
        return call

    @property
    def total_tokens(self) -> int:
        return sum(c["input_tokens"] + c["output_tokens"] for c in self.calls)

    def totals(self) -> dict:
        by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        for call in self.calls:
            entry = by_model[call["model"]]
            entry["calls"] += 1
            entry["input_tokens"] += call["input_tokens"]
            entry["output_tokens"] += call["output_tokens"]
        costs = [c["cost_usd"] for c in self.calls]
        return {
            "calls": len(self.calls),
            "input_tokens": sum(c["input_tokens"] for c in self.calls),
            "output_tokens": sum(c["output_tokens"] for c in self.calls),
            "total_tokens": self.total_tokens,
            # Unknown if any model has no price configured.
            "cost_usd": round(sum(costs), 6) if costs and None not in costs else None,
            "latency_ms": round(sum(c["latency_ms"] for c in self.calls), 1),
            "by_model": dict(by_model),
        }
