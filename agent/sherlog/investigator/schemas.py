"""Structured outputs of the triage and investigator models.

The pydantic models validate what a model returns; `json_schema()` turns them
into a self-contained JSON schema (no $refs, every field required, no extra
keys) to send as the structured-output contract.
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CATEGORIES = (
    "bad_deploy_code_bug", "config_change", "memory_exhaustion", "cpu_exhaustion", "db_slow_query",
    "db_pool_exhaustion", "network_latency", "network_partition", "dependency_down", "process_crash",
    "process_hang", "retry_storm", "unknown",
)
# What each category means, so the model picks between close ones on purpose.
CATEGORY_MEANING = {
    "bad_deploy_code_bug": "a recent deploy shipped code that fails",
    "config_change": "a configuration change or feature flag broke behavior",
    "memory_exhaustion": "a process runs out of memory (growth, OOM kills)",
    "cpu_exhaustion": "a process is CPU-bound and slows everything it serves",
    "db_slow_query": "database queries got slow",
    "db_pool_exhaustion": "a service runs out of database connections",
    "network_latency": "calls between services are slow on the network, not in the callee's code",
    "network_partition": "calls between services fail to connect or are dropped",
    "dependency_down": "a datastore or other dependency (database, cache, queue, external API) is unavailable",
    "process_crash": "one of the shop's own services died or keeps restarting",
    "process_hang": "a service is up but stops answering",
    "retry_storm": "retries multiply load until it overwhelms a service",
    "unknown": "the evidence doesn't support any of the above",
}
assert set(CATEGORY_MEANING) == set(CATEGORIES)
ACTIONS = ("rollback", "flag_off", "restart", "scale", "config_revert", "open_fix_pr", "escalate")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RootCause(Strict):
    service: str
    category: Literal[CATEGORIES]
    summary: str
    confidence: float = Field(ge=0, le=1)


class EvidenceItem(Strict):
    type: Literal["metric", "log", "trace", "exception", "change", "code"]
    ref: str
    detail: str


class RuledOut(Strict):
    hypothesis: str
    reason: str


class ActionParams(Strict):
    service: str | None
    flag: str | None
    replicas: int | None


class Action(Strict):
    type: Literal[ACTIONS]
    params: ActionParams
    risk: Literal["low", "medium", "high"]


class Diagnosis(Strict):
    root_cause: RootCause
    evidence: list[EvidenceItem]
    ruled_out: list[RuledOut]
    recommended_action: Action


class ServiceTriage(Strict):
    service: str
    assessment: Literal["likely_cause", "affected", "noise", "healthy"]
    what_changed: list[str]
    new_errors: list[str]
    likely_noise: list[str]


class Triage(Strict):
    overview: str
    services: list[ServiceTriage]


def json_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].rsplit("/", 1)[-1]])
            node = {k: inline(v) for k, v in node.items() if k not in ("title", "default")}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    return inline(schema)


FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse(model: type[BaseModel], content: str | None):
    """Validate a model reply; tolerate code fences and text around the JSON object."""
    if not content:
        raise ValueError("empty reply")
    text = FENCE.sub("", content.strip())
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("no JSON object in the reply")
        text = text[start : end + 1]
    try:
        return model.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc)[:600]) from exc
