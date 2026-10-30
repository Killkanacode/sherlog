"""Stand-in models for local development without API access (SHERLOG_FAKE_LLM).

They exercise the real plumbing (tool calls, JSON output, token accounting,
streaming) but do no reasoning: the fake investigator runs a fixed sequence of
tools and then repeats the heuristic hint with category "unknown" and zero
confidence, so a fake result can never pass for a diagnosis.
"""

import json
import re

from sherlog.llm.base import Completion, ToolCall

JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def _payload(messages: list[dict]) -> dict:
    """The evidence pack: the JSON block in the user's messages that has a root-cause hint."""
    for message in messages:
        if message["role"] != "user":
            continue
        for block in JSON_BLOCK.findall(message.get("content") or ""):
            data = json.loads(block)
            if isinstance(data, dict) and "root_cause_hint" in data:
                return data
    return {}


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _completion(messages: list[dict], content: str | None, tool_calls: list[ToolCall] | None = None) -> Completion:
    sent = json.dumps(messages, default=str)
    out = (content or "") + json.dumps([c.__dict__ for c in tool_calls or []])
    return Completion("fake", content, tool_calls or [], _tokens(sent), _tokens(out), 1.0)


class FakeTriageModel:
    model = "fake-triage"
    price_in = price_out = 0.0

    async def complete(self, messages, *, tools=None, schema=None, max_tokens=2048) -> Completion:
        evidence = _payload(messages)
        hint = (evidence.get("root_cause_hint") or {}).get("service")
        services = []
        for service in evidence.get("suspect_services", []):
            services.append(
                {
                    "service": service,
                    "assessment": "likely_cause" if service == hint else "affected",
                    "what_changed": [
                        f"{c['type']} by {c.get('actor')}" for c in evidence.get("changes", []) if c.get("service") == service
                    ],
                    "new_errors": [
                        t["template"][:120] for t in evidence.get("log_templates", []) if t["service"] == service and t["new"]
                    ],
                    "likely_noise": [],
                }
            )
        report = {"overview": f"Fake triage: the graph heuristic points at {hint}.", "services": services}
        return _completion(messages, json.dumps(report))


class FakeInvestigatorModel:
    model = "fake-investigator"
    price_in = price_out = 0.0

    async def complete(self, messages, *, tools=None, schema=None, max_tokens=2048) -> Completion:
        evidence = _payload(messages)
        hint = (evidence.get("root_cause_hint") or {}).get("service") or "unknown"
        turns = sum(1 for m in messages if m["role"] == "assistant")
        if tools and turns == 0:
            return _completion(
                messages,
                f"Fake hypothesis: the heuristic hint ({hint}) is the cause. Checking its health and recent changes.",
                [
                    ToolCall("fake-1", "get_service_health", {"service": hint}),
                    ToolCall("fake-2", "get_recent_changes", {"service": hint}),
                ],
            )
        if tools and turns == 1:
            exceptions = [e for e in evidence.get("exceptions", []) if e["service"] == hint]
            call = (
                ToolCall("fake-3", "get_exception", {"fingerprint": exceptions[0]["fingerprint"]})
                if exceptions
                else ToolCall("fake-3", "get_dependencies", {"service": hint})
            )
            return _completion(messages, "Fake hypothesis: looking one level deeper.", [call])
        used = [c["name"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or []]
        report = {
            "root_cause": {
                "service": hint,
                "category": "unknown",
                "summary": "Fake model: this repeats the graph heuristic. Connect a real model for a diagnosis.",
                "confidence": 0.0,
            },
            "evidence": [{"type": "metric", "ref": name, "detail": "tool output seen by the fake model"} for name in used],
            "ruled_out": [],
            "recommended_action": {
                "type": "escalate",
                "params": {"service": hint, "flag": None, "replicas": None},
                "risk": "low",
            },
        }
        return _completion(messages, json.dumps(report))
