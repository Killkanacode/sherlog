import asyncio
import dataclasses
import json

import httpx
import pytest

from sherlog.investigator import agent, tools
from sherlog.investigator.schemas import Diagnosis, Triage, json_schema, parse
from sherlog.llm import fake
from sherlog.llm.base import Completion, ToolCall
from sherlog.llm.chat_api import ChatCompletionsModel
from sherlog.llm.usage import Usage

EVIDENCE = {
    "window": {"start": "2026-10-30T12:00:00+00:00", "end": "2026-10-30T12:05:00+00:00"},
    "suspect_services": ["gateway", "orders", "payments"],
    "root_cause_hint": {"service": "payments", "reason": "deepest anomalous service", "ranking": []},
    "signals": [],
    "changes": [{"type": "deploy", "service": "payments", "actor": "dev.kumar"}],
    "log_templates": [],
    "exceptions": [{"fingerprint": "abc123", "service": "payments", "type": "TypeError"}],
    "traces": [],
    "trace_logs": [],
    "stats": {},
    "topology": [],
}
INCIDENT = {"id": 7, "title": "error rate on payments", "services": ["payments", "orders", "gateway"]}
VALID = {
    "root_cause": {"service": "payments", "category": "bad_deploy_code_bug", "summary": "None not checked", "confidence": 0.9},
    "evidence": [{"type": "exception", "ref": "abc123", "detail": "TypeError in charge"}],
    "ruled_out": [{"hypothesis": "card provider down", "reason": "provider latency normal"}],
    "recommended_action": {"type": "rollback", "params": {"service": "payments", "flag": None, "replicas": None}, "risk": "low"},
}


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    async def insert(incident_id, call):
        pass

    monkeypatch.setattr("sherlog.db.insert_llm_call", insert)


@pytest.fixture
def stub_tools(monkeypatch):
    called = []

    async def run_tool(ctx, name, arguments):
        called.append((name, arguments))
        return {"ok": name}

    monkeypatch.setattr(agent, "run_tool", run_tool)
    return called


def run(coro):
    return asyncio.run(coro)


def test_schema_is_self_contained_and_strict():
    schema = json_schema(Diagnosis)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"root_cause", "evidence", "ruled_out", "recommended_action"}
    category = schema["properties"]["root_cause"]["properties"]["category"]
    assert "retry_storm" in category["enum"] and "unknown" in category["enum"]


def test_parse_tolerates_fences_and_prose():
    content = "Here is my answer:\n```json\n" + json.dumps(VALID) + "\n```"
    assert parse(Diagnosis, content).root_cause.service == "payments"


def test_parse_rejects_unknown_category():
    bad = json.loads(json.dumps(VALID))
    bad["root_cause"]["category"] = "cosmic_rays"
    with pytest.raises(ValueError):
        parse(Diagnosis, json.dumps(bad))


def test_fake_investigation_runs_tools_and_never_claims_a_diagnosis(stub_tools):
    events, usage = [], Usage(7)

    async def emit(kind, text, data):
        events.append(kind)

    triage = {"overview": "x", "services": []}
    diagnosis = run(agent.investigate(fake.FakeInvestigatorModel(), INCIDENT, EVIDENCE, triage, usage, emit))
    assert stub_tools == [
        ("get_service_health", {"service": "payments"}),
        ("get_recent_changes", {"service": "payments"}),
        ("get_exception", {"fingerprint": "abc123"}),
    ]
    assert diagnosis["root_cause"]["category"] == "unknown"
    assert diagnosis["root_cause"]["confidence"] == 0.0
    assert events.count("tool") == 3 and "hypothesis" in events
    assert usage.totals()["calls"] == 3 and usage.total_tokens > 0


class Scripted:
    """A model that replays canned completions."""

    model, price_in, price_out = "scripted", 1.0, 2.0

    def __init__(self, *replies: Completion) -> None:
        self.replies, self.requests = list(replies), []

    async def complete(self, messages, *, tools=None, schema=None, max_tokens=2048):
        self.requests.append({"tools": bool(tools), "schema": bool(schema)})
        return self.replies.pop(0)


async def _noop(kind, text, data):
    pass


def test_invalid_answer_is_retried_with_structured_output(stub_tools):
    model = Scripted(
        Completion("scripted", "I think it's payments.", input_tokens=1000, output_tokens=10),
        Completion("scripted", json.dumps(VALID), input_tokens=1100, output_tokens=200),
    )
    usage = Usage(7)
    diagnosis = run(agent.investigate(model, INCIDENT, EVIDENCE, None, usage, _noop))
    assert diagnosis["recommended_action"]["type"] == "rollback"
    assert model.requests == [{"tools": True, "schema": False}, {"tools": False, "schema": True}]
    assert usage.totals()["cost_usd"] == pytest.approx((2100 * 1.0 + 210 * 2.0) / 1e6)


def test_step_limit_forces_a_conclusion(stub_tools, monkeypatch):
    monkeypatch.setattr(agent, "settings", dataclasses.replace(agent.settings, max_steps=3))
    looping = [Completion("scripted", None, [ToolCall(f"c{i}", "get_dependencies", {"service": "orders"})]) for i in range(2)]
    model = Scripted(*looping, Completion("scripted", json.dumps(VALID)))
    run(agent.investigate(model, INCIDENT, EVIDENCE, None, Usage(7), _noop))
    assert len(model.requests) == 3 and model.requests[-1] == {"tools": False, "schema": True}


def test_unusable_model_falls_back_to_escalation(stub_tools):
    model = Scripted(Completion("scripted", "no idea"), Completion("scripted", "still no idea"))
    diagnosis = run(agent.investigate(model, INCIDENT, EVIDENCE, None, Usage(7), _noop))
    assert diagnosis["recommended_action"]["type"] == "escalate"
    assert diagnosis["root_cause"] == {
        "service": "payments", "category": "unknown", "summary": diagnosis["root_cause"]["summary"], "confidence": 0.0
    }


def test_fake_triage_output_matches_schema():
    completion = run(fake.FakeTriageModel().complete([{"role": "user", "content": "```json\n" + json.dumps(EVIDENCE) + "\n```"}]))
    triage = parse(Triage, completion.content)
    assert {s.service: s.assessment for s in triage.services}["payments"] == "likely_cause"


def test_tool_arguments_are_checked_before_running():
    ctx = tools.ToolContext(7, None)
    assert "unknown tool" in run(tools.run_tool(ctx, "rm_rf", {}))["error"]
    assert "missing ['service']" in run(tools.run_tool(ctx, "get_dependencies", {}))["error"]
    assert "unknown ['force']" in run(tools.run_tool(ctx, "get_dependencies", {"service": "orders", "force": True}))["error"]
    assert "must be one of" in run(tools.run_tool(ctx, "get_dependencies", {"service": "unknown"}))["error"]


def test_every_tool_spec_is_a_closed_object():
    names = {spec["name"] for spec in tools.specs()}
    assert names == {
        "query_metrics", "get_service_health", "get_traces", "get_logs", "get_exception", "get_code_context",
        "get_recent_changes", "diff_deploy", "get_dependencies", "search_past_incidents",
    }
    for spec in tools.specs():
        assert spec["parameters"]["additionalProperties"] is False
        assert set(spec["parameters"]["required"]) <= set(spec["parameters"]["properties"])


def test_chat_api_wire_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["auth"], seen["body"] = request.url.path, request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={
            "model": "m-1",
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "get_dependencies", "arguments": "{\"service\": \"orders\"}"}}
            ]}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        })

    model = ChatCompletionsModel("https://api.test.invalid/v1", "k", "m-1", timeout_s=5, reasoning_effort="low")
    model._http = httpx.AsyncClient(base_url="https://api.test.invalid/v1", transport=httpx.MockTransport(handler),
                                    headers={"Authorization": "Bearer k"})
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t0", "name": "get_exception", "arguments": {"fingerprint": "x"}}]},
        {"role": "tool", "tool_call_id": "t0", "content": "{}"},
    ]
    completion = run(model.complete(history, tools=tools.specs()[:1], max_tokens=8192))
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["body"]["messages"][1]["tool_calls"][0]["function"]["arguments"] == "{\"fingerprint\": \"x\"}"
    assert seen["body"]["tools"][0]["type"] == "function"
    # max_tokens is deprecated on the Meta API; the cap covers reasoning too.
    assert seen["body"]["max_completion_tokens"] == 8192 and "max_tokens" not in seen["body"]
    assert seen["body"]["reasoning_effort"] == "low"
    assert completion.tool_calls[0].arguments == {"service": "orders"}
    assert (completion.input_tokens, completion.output_tokens) == (120, 30)

    # Partner endpoints may not know reasoning_effort: only sent when set.
    plain = ChatCompletionsModel("https://api.test.invalid/v1", "k", "m-1", timeout_s=5)
    plain._http = model._http
    run(plain.complete([{"role": "user", "content": "hi"}], schema={"type": "object"}))
    assert "reasoning_effort" not in seen["body"]
    assert seen["body"]["response_format"]["type"] == "json_schema"


def test_triage_falls_back_to_muse_spark_without_an_open_model(monkeypatch):
    import dataclasses

    from sherlog import llm

    patched = dataclasses.replace(
        llm.settings, fake_llm=False, meta_api_key="k", open_model_base_url="", open_model_api_key="",
    )
    monkeypatch.setattr(llm, "settings", patched)
    llm.triage_model.cache_clear()
    try:
        triage = llm.triage_model().inner
        assert triage.model == patched.model_investigator and triage.reasoning_effort == "minimal"
        assert "not set" in llm.describe()["triage"]
    finally:
        llm.triage_model.cache_clear()
