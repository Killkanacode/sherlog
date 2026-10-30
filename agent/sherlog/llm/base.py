"""The one interface every model client implements.

Messages use the common chat shape: {"role": system|user|assistant|tool,
"content": str, "tool_calls": [...], "tool_call_id": str}. Tools are
{"name", "description", "parameters": <JSON schema>}. A client translates
both to whatever its API expects, so the rest of Sherlog never changes when
a provider does.
"""

from dataclasses import dataclass, field
from typing import Protocol


class LLMError(Exception):
    """The model call failed (network, auth, quota, or a malformed reply)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Completion:
    model: str
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class ChatModel(Protocol):
    model: str
    price_in: float | None  # USD per million input tokens, None if unknown
    price_out: float | None

    async def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        schema: dict | None = None,
        max_tokens: int = 2048,
    ) -> Completion:
        """One model turn. With `tools`, the reply may carry tool calls; with
        `schema`, the reply content must be JSON matching it."""
        ...


def cost_usd(model: ChatModel, completion: Completion) -> float | None:
    if model.price_in is None or model.price_out is None:
        return None
    return (completion.input_tokens * model.price_in + completion.output_tokens * model.price_out) / 1_000_000
