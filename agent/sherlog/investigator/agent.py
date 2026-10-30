"""Investigator (Muse Spark with tool calling).

A bounded agent loop: the model reads the evidence pack and the triage, states
a hypothesis, calls tools to test it, and ends with a JSON diagnosis. Limits:
SHERLOG_MAX_STEPS model turns, SHERLOG_TOKEN_BUDGET_PER_INCIDENT tokens and a
timeout per tool. Every step is reported through `emit` for the live UI.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sherlog.config import settings
from sherlog.investigator.schemas import ACTIONS, CATEGORIES, Diagnosis, json_schema, parse
from sherlog.investigator.tools import ToolContext, render, run_tool, specs
from sherlog.llm import ChatModel, LLMError
from sherlog.llm.usage import Usage

log = logging.getLogger("sherlog.investigator")

Emit = Callable[[str, str, dict], Awaitable[None]]

MAX_PARALLEL_TOOLS = 4
BUDGET_MARGIN = 0.85  # stop calling tools once this share of the token budget is spent

SYSTEM = f"""You are Sherlog, an autonomous on-call SRE investigating a live production incident.

The system is an online shop: gateway -> orders -> inventory and payments. orders and inventory use
PostgreSQL, payments and inventory use Redis, and payments calls an external card provider. Services
are deployed by a platform that records every deploy, config change and feature flag as a change event.

Your job: find the root cause (the service where the fault originates, not every service that hurts)
and its category, backed by evidence, and recommend the safest action that would fix it.

How to work:
- Start from the evidence pack and the triage. The root-cause hint is a graph heuristic: useful, not proof.
- Before each tool call, state your current hypothesis in one sentence.
- Separate cause from victims: callers of a broken service fail too, and a service that loses traffic
  or goes quiet may only be starved by a broken caller.
- Change events right before the incident are prime suspects, but some changes are harmless noise.
  Confirm with code (diff_deploy, get_code_context), exceptions, traces or metrics before blaming one.
- Be efficient: you have at most {settings.max_steps} turns. Stop as soon as the evidence is conclusive.

Categories: {", ".join(CATEGORIES)}.
Actions: {", ".join(ACTIONS)}. rollback undoes the service's last deploy; config_revert undoes its last
config change; flag_off needs params.flag; scale needs params.replicas; open_fix_pr proposes a code fix;
escalate hands over to a human when the cause is unclear or no safe action exists.

When you are done, reply with only a JSON object matching this schema (no tool call, no prose):
{json.dumps(json_schema(Diagnosis))}"""


def investigation_input(incident: dict, evidence: dict, triage: dict | None) -> str:
    pack = {k: v for k, v in evidence.items() if k not in ("stats", "topology")}
    hint = pack.get("root_cause_hint") or {}
    pack["root_cause_hint"] = {"service": hint.get("service"), "reason": hint.get("reason")}
    parts = [
        f"Incident #{incident['id']}: {incident['title']} (services: {', '.join(incident['services'])}).",
        "Triage:\n```json\n" + json.dumps(triage) + "\n```" if triage else "Triage: unavailable.",
        "Evidence pack:\n```json\n" + json.dumps(pack, default=str) + "\n```",
    ]
    return "\n\n".join(parts)


def fallback(reason: str, service: str | None) -> dict:
    return {
        "root_cause": {"service": service or "unknown", "category": "unknown", "summary": reason, "confidence": 0.0},
        "evidence": [],
        "ruled_out": [],
        "recommended_action": {
            "type": "escalate", "params": {"service": service, "flag": None, "replicas": None}, "risk": "low"
        },
    }


async def investigate(
    model: ChatModel, incident: dict, evidence: dict, triage: dict | None, usage: Usage, emit: Emit
) -> dict:
    ctx = ToolContext(incident["id"], datetime.fromisoformat(evidence["window"]["start"]))
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": investigation_input(incident, evidence, triage)},
    ]
    hint = (evidence.get("root_cause_hint") or {}).get("service")

    for step in range(1, settings.max_steps):  # the closing answer below may take the last turn
        if usage.total_tokens >= settings.token_budget * BUDGET_MARGIN:
            await emit("budget", f"Token budget nearly spent ({usage.total_tokens:,} tokens); concluding", {})
            break
        completion = await model.complete(messages, tools=specs(), max_tokens=2048)
        await usage.record("investigator", model, completion)

        if not completion.tool_calls:
            try:
                return parse(Diagnosis, completion.content).model_dump()
            except ValueError as exc:
                await emit("repair", "The answer was not a valid diagnosis; asking again with the schema", {"error": str(exc)})
                messages.append({"role": "assistant", "content": completion.content})
                break

        calls = completion.tool_calls[:MAX_PARALLEL_TOOLS]
        if completion.content:
            await emit("hypothesis", completion.content.strip(), {"step": step})
        messages.append(
            {
                "role": "assistant",
                "content": completion.content,
                "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
            }
        )
        for call in calls:
            result = await run_tool(ctx, call.name, call.arguments)
            rendered = render(result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": rendered})
            failed = isinstance(result, dict) and "error" in result
            await emit(
                "tool",
                f"{call.name}({_args(call.arguments)})" + (f" -> error: {result['error']}" if failed else ""),
                {"step": step, "tool": call.name, "arguments": call.arguments, "result": rendered[:2000]},
            )

    # Out of turns or budget, or the free-form answer didn't validate: ask for the
    # diagnosis once more with structured output and no tools.
    messages.append({"role": "user", "content": "Stop investigating. Reply now with the final JSON diagnosis."})
    try:
        completion = await model.complete(messages, schema=json_schema(Diagnosis), max_tokens=2048)
        await usage.record("investigator", model, completion)
        return parse(Diagnosis, completion.content).model_dump()
    except (LLMError, ValueError) as exc:
        log.warning("Incident #%d: no valid diagnosis: %s", incident["id"], exc)
        return fallback(f"The investigator could not produce a valid diagnosis: {exc}", hint)


def _args(arguments: dict) -> str:
    text = ", ".join(f"{k}={json.dumps(v)}" for k, v in arguments.items())
    return text if len(text) <= 160 else text[:157] + "..."
