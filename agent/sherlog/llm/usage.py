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
        call = {
            "ts": utcnow().isoformat(),
            "role": role,
            "model": completion.model or model.model,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "latency_ms": completion.latency_ms,
            "cost_usd": cost_usd(model, completion),
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
