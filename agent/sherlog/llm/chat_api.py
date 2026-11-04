"""HTTP client for a chat-completions style API.

Checked against Meta's Model API docs (dev.meta.ai/docs, 2026-11-04) and a
live call to muse-spark-1.3: `POST {base}/chat/completions` with a bearer
token; OpenAI-style tools and tool calls (several per turn); structured output
through `response_format` of type `json_schema`; usage in `prompt_tokens` and
`completion_tokens`.

Muse Spark always reasons before it answers, and `max_completion_tokens`
caps reasoning plus visible output together, so limits must leave room for
both. `reasoning_effort` (minimal..max; reasoning can't be turned off) trades
depth for latency. Partner endpoints for Muse Spark Open are OpenAI-compatible
too, but may not accept `reasoning_effort`, so it is only sent when set.
"""

import asyncio
import json
import logging
import time

import httpx

from sherlog.llm.base import Completion, LLMError, ToolCall

log = logging.getLogger("sherlog.llm")

RETRY_STATUS = {429, 500, 502, 503, 504}
RETRIES = 2


class ChatCompletionsModel:
    def __init__(
        self, base_url: str, api_key: str, model: str, *, timeout_s: float,
        price_in: float | None = None, price_out: float | None = None, reasoning_effort: str = "",
    ) -> None:
        if not base_url or not api_key or not model:
            raise LLMError(f"model {model or '<unset>'} needs a base URL, an API key and a model ID")
        self.model = model
        self.price_in, self.price_out = price_in, price_out
        self.reasoning_effort = reasoning_effort
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )

    async def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        schema: dict | None = None,
        max_tokens: int = 2048,
    ) -> Completion:
        # `max_tokens` is deprecated on the Meta API; the cap includes reasoning.
        body: dict = {
            "model": self.model, "messages": [_to_wire(m) for m in messages], "max_completion_tokens": max_tokens,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = [{"type": "function", "function": tool} for tool in tools]
            body["tool_choice"] = "auto"
        if schema:
            # Output follows the schema either way; strict only adds server-side
            # validation of the schema itself, which would turn a schema quirk
            # into an HTTP 400 in the middle of an incident.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": False},
            }

        started = time.monotonic()
        data = await self._post("/chat/completions", body)
        latency_ms = (time.monotonic() - started) * 1000
        try:
            message = data["choices"][0]["message"]
            usage = data.get("usage") or {}
            return Completion(
                model=data.get("model", self.model),
                content=message.get("content"),
                tool_calls=[_from_wire(call) for call in message.get("tool_calls") or []],
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                latency_ms=round(latency_ms, 1),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"unexpected response shape from {self.model}: {str(data)[:300]}") from exc

    async def _post(self, path: str, body: dict) -> dict:
        for attempt in range(RETRIES + 1):
            try:
                response = await self._http.post(path, json=body)
            except httpx.HTTPError as exc:
                if attempt == RETRIES:
                    raise LLMError(f"{self.model}: {exc}") from exc
            else:
                if response.status_code < 400:
                    return response.json()
                if response.status_code not in RETRY_STATUS or attempt == RETRIES:
                    raise LLMError(f"{self.model}: HTTP {response.status_code}: {response.text[:300]}")
            await asyncio.sleep(2 ** attempt)
        raise AssertionError("unreachable")


def _to_wire(message: dict) -> dict:
    wire = {"role": message["role"], "content": message.get("content")}
    if message.get("tool_calls"):
        wire["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
            for c in message["tool_calls"]
        ]
    if message.get("tool_call_id"):
        wire["tool_call_id"] = message["tool_call_id"]
    return wire


def _from_wire(call: dict) -> ToolCall:
    function = call["function"]
    arguments = function.get("arguments") or "{}"
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        log.warning("Tool call %s has malformed arguments: %s", function.get("name"), arguments[:200])
        parsed = {"_malformed": arguments}
    return ToolCall(id=call.get("id", ""), name=function["name"], arguments=parsed)
