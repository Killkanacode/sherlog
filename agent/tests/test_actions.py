import asyncio
import dataclasses
import json
from datetime import timedelta

import httpx
import pytest

from sherlog import db
from sherlog.actions import execute as actions
from sherlog.actions import verify
from sherlog.actions.policy import policy
from sherlog.detectors.logs import Counter, Exception_, log_detector
from sherlog.detectors.metrics import SeriesState, metric_detector
from sherlog.incidents import manager as manager_module
from sherlog.incidents.manager import ActionError, Incident, IncidentManager
from sherlog.ingest.clients import PlatformError, platform
from sherlog.memory import store as memory
from sherlog.postmortem import writer as postmortem
from sherlog.signals import Signal, utcnow

EVIDENCE = {
    "window": {"start": "2026-10-30T12:00:00+00:00", "end": "2026-10-30T12:05:00+00:00"},
    "suspect_services": ["gateway", "orders"],
    "root_cause_hint": {"service": "orders", "reason": "deepest anomalous service", "ranking": []},
    "signals": [],
    "changes": [],
    "log_templates": [
        {"template": "checkout failed: <*>", "level": "ERROR", "service": "orders"},
        {"template": "GET /products <*>", "level": "INFO", "service": "inventory"},
    ],
    "exceptions": [{"fingerprint": "fp-keyerror", "service": "orders", "type": "KeyError"}],
    "traces": [],
    "trace_logs": [],
    "stats": {"log_lines_in_window": 12000, "templates_selected": 1, "exceptions_selected": 1,
              "traces_selected": 0, "estimated_tokens": 2400},
    "topology": [],
}


def run(coro):
    return asyncio.run(coro)


def diagnosis(action: str, service: str | None, confidence: float = 0.9, risk: str = "low") -> dict:
    return {
        "root_cause": {"service": "orders", "category": "bad_deploy_code_bug", "summary": "KeyError on price",
                       "confidence": confidence},
        "evidence": [{"type": "exception", "ref": "fp-keyerror", "detail": "KeyError: 'price' in order_total"}],
        "ruled_out": [{"hypothesis": "payments down", "reason": "payments healthy"}],
        "recommended_action": {"type": action, "params": {"service": service, "flag": None, "replicas": None},
                               "risk": risk},
    }


def incident(incident_id: int = 41) -> Incident:
    started = utcnow() - timedelta(minutes=3)
    signals = [
        Signal("metric_anomaly", "orders", "critical", started,
               {"metric": "error_rate", "direction": "up", "value": 0.4, "baseline": 0.0, "impact": True},
               "metric:error_rate:orders"),
        Signal("new_exception", "orders", "critical", started + timedelta(seconds=5),
               {"fingerprint": "fp-keyerror", "exception": "KeyError", "message": "'price'"}, "exc:fp-keyerror"),
    ]
    found = Incident(id=incident_id, created_at=started + timedelta(seconds=30), services=["orders", "gateway"],
                     signals=signals)
    found.evidence = json.loads(json.dumps(EVIDENCE))
    return found


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No database, no platform, no real waiting; policy restored afterwards."""
    saved = dataclasses.replace(policy)
    record = {"remembered": [], "executed": [], "reverted": []}

    async def nothing(*args, **kwargs):
        return None

    async def remember(row):
        record["remembered"].append(row)

    async def recall(*args, **kwargs):
        return []

    async def impact(start, end):
        return {"requests": 1000, "failed": 120}

    for name in ("update_incident", "put_setting", "insert_llm_call"):
        monkeypatch.setattr(db, name, nothing)
    monkeypatch.setattr(db, "remember", remember)
    monkeypatch.setattr(db, "recall", recall)
    monkeypatch.setattr(postmortem, "gateway_impact", impact)
    yield record
    policy.autonomy, policy.dry_run = saved.autonomy, saved.dry_run


@pytest.fixture
def platform_calls(monkeypatch, isolated):
    """actions.execute/revert stubbed; verification outcomes are queued per test."""
    outcomes = []

    async def execute(type_, params):
        isolated["executed"].append((type_, params))
        undo = None if type_ == "restart" else {
            "description": f"undo {type_} {params.get('service')}", "method": "POST", "path": "/x", "body": {}
        }
        return {"ok": True}, undo

    async def revert(undo):
        isolated["reverted"].append(undo["description"])
        return {"ok": True}

    async def fake_verify(services, fingerprints, on_check=None):
        outcome = outcomes.pop(0)
        now = utcnow().isoformat()
        return {"outcome": outcome, "started_at": now, "recovered_at": now if outcome == "recovered" else None,
                "finished_at": now, "seconds": 45, "checks": 5,
                "remaining": [] if outcome == "recovered" else
                [{"metric": "error_rate", "service": "orders", "peer": None, "value": 0.38, "baseline": 0.0}]}

    monkeypatch.setattr(actions, "execute", execute)
    monkeypatch.setattr(actions, "revert", revert)
    monkeypatch.setattr(verify, "verify", fake_verify)
    return outcomes


async def settle(manager: IncidentManager) -> None:
    while manager._tasks:
        await asyncio.gather(*list(manager._tasks))


# ------------------------------------------------------------------ whitelist and policy
def test_whitelist_refuses_unknown_actions_services_and_params():
    assert actions.check("rollback", {"service": "orders"}) is None
    assert "not an action" in actions.check("drop_database", {"service": "orders"})
    assert "not allowed on postgres" in actions.check("rollback", {"service": "postgres"})
    assert actions.check("restart", {"service": "redis"}) is None
    assert "needs params.flag" in actions.check("flag_off", {})
    assert "replicas" in actions.check("scale", {"service": "orders", "replicas": 9})


def test_restarting_a_datastore_is_never_low_risk():
    assert actions.risk("restart", {"service": "redis"}, "low") == "medium"
    assert actions.risk("restart", {"service": "orders"}, "low") == "low"
    assert actions.risk("rollback", {"service": "orders"}, "high") == "high"


@pytest.mark.parametrize(
    "autonomy, action, risk, confidence, expected",
    [
        ("L0", "rollback", "low", 0.95, "suggest"),
        ("L1", "rollback", "low", 0.95, "approve"),
        ("L2", "rollback", "low", 0.95, "auto"),
        ("L2", "rollback", "low", 0.4, "approve"),  # not confident enough
        ("L2", "rollback", "medium", 0.95, "approve"),
        ("L2", "scale", "low", 0.95, "approve"),  # not one of the automatic actions
    ],
)
def test_autonomy_levels(autonomy, action, risk, confidence, expected):
    policy.autonomy = autonomy
    assert policy.decide({"type": action, "risk": risk}, confidence) == expected


def test_settings_reject_an_unknown_level():
    with pytest.raises(ValueError):
        run(policy.update(autonomy="L9"))
    assert run(policy.update(autonomy="L2", dry_run=True))["autonomy"] == "L2" and policy.dry_run


# ------------------------------------------------------------------ executors
def mock_platform(monkeypatch, routes: dict) -> list:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        status, payload = routes.get((request.method, request.url.path), (200, {}))
        return httpx.Response(status, json=payload)

    client = httpx.AsyncClient(base_url="http://platform.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(platform, "_http", client)
    return seen


def test_rollback_records_how_to_undo_it(monkeypatch):
    seen = mock_platform(monkeypatch, {
        ("GET", "/services/orders/history"): (200, [{"version": "aaa1111", "commit_sha": "aaa1111" * 4},
                                                    {"version": "bbb2222", "commit_sha": "bbb2222" * 4}]),
        ("POST", "/rollback"): (200, {"service": "orders", "version": "aaa1111"}),
    })
    result, undo = run(actions.execute("rollback", {"service": "orders"}))
    assert seen[-1] == ("POST", "/rollback", {"actor": "sherlog", "service": "orders"})
    assert result["version"] == "aaa1111"
    assert (undo["method"], undo["path"], undo["body"]) == ("POST", "/deploy", {"service": "orders", "ref": "bbb2222" * 4})


def test_config_revert_undo_skips_masked_values(monkeypatch):
    mock_platform(monkeypatch, {("POST", "/config/orders/revert"): (200, {"changes": {
        "RETRIES": {"from": "10", "to": "2"}, "DB_PASSWORD": {"from": "***", "to": "***"}, "NEW": {"from": None, "to": "1"},
    }})})
    _, undo = run(actions.execute("config_revert", {"service": "orders"}))
    assert undo["body"] == {"env": {"RETRIES": "10"}}


def test_flag_off_only_undoes_a_flag_that_was_on(monkeypatch):
    mock_platform(monkeypatch, {("GET", "/flags"): (200, {"new_pricing": False})})
    assert run(actions.execute("flag_off", {"flag": "new_pricing"}))[1] is None


def test_platform_refusal_raises(monkeypatch):
    mock_platform(monkeypatch, {("POST", "/restart"): (403, {"detail": "nope"})})
    with pytest.raises(PlatformError, match="403"):
        run(actions.execute("restart", {"service": "orders"}))


# ------------------------------------------------------------------ verification
def test_problems_reads_metric_baselines_signals_and_exceptions(monkeypatch):
    def series(value: float) -> SeriesState:
        state = SeriesState()
        for _ in range(40):
            state.baseline.update(0.0)
        state.last_value = value
        return state

    monkeypatch.setattr(metric_detector, "series", {
        ("error_rate", "orders"): series(0.5),
        ("error_rate", "payments"): series(0.0),
        ("traffic", "orders"): series(50.0),  # not an impact metric
    })
    counter = Counter()
    counter.add(6)
    exc = Exception_("fp-keyerror", "orders", "KeyError", "'price'", [], "", "abc", utcnow(), False, counter)
    monkeypatch.setattr(log_detector, "exceptions", {"fp-keyerror": exc})
    found = verify.problems({"orders", "payments"}, {"fp-keyerror"})
    assert [p.get("metric") or p.get("exception") for p in found] == ["error_rate", "KeyError"]
    assert "error_rate on orders at 0.5" in verify.describe(found)


def test_verify_needs_a_healthy_stretch(monkeypatch):
    fast = dataclasses.replace(verify.settings, verify_timeout_s=1, verify_healthy_s=0.05, verify_poll_s=0.02)
    monkeypatch.setattr(verify, "settings", fast)
    states = iter([[{"metric": "error_rate"}], [], [], [], [], []])
    monkeypatch.setattr(verify, "problems", lambda services, fps: next(states, []))
    assert run(verify.verify({"orders"}, set()))["outcome"] == "recovered"

    monkeypatch.setattr(verify, "problems", lambda services, fps: [{"signal": "not_ready", "service": "orders"}])
    result = run(verify.verify({"orders"}, set()))
    assert result["outcome"] == "not_recovered" and result["remaining"][0]["signal"] == "not_ready"


# ------------------------------------------------------------------ the incident loop
def test_l1_waits_for_approval_then_verifies_closes_and_remembers(platform_calls, isolated):
    policy.autonomy, policy.dry_run = "L1", False
    platform_calls.append("recovered")
    found = incident()
    found.diagnosis = diagnosis("rollback", "orders")

    async def scenario():
        manager = IncidentManager()
        manager.incidents[found.id] = found
        await manager.respond(found)
        assert found.status == "mitigating" and found.pending_action["state"] == "awaiting_approval"
        assert not isolated["executed"]
        await manager.approve(found, "miguel")
        with pytest.raises(ActionError):
            await manager.approve(found, "miguel")
        await settle(manager)

    run(scenario())
    assert isolated["executed"] == [("rollback", {"service": "orders"})]
    assert found.status == "resolved" and found.resolved_at and found.actions[0]["state"] == "recovered"
    assert found.actions[0]["approved_by"] == "miguel"
    text = found.postmortem
    assert "Resolved by Sherlog: roll back orders to its previous version" in text
    assert "120 of 1,000 shop requests failed (12.0%)" in text
    for section in ("## Summary", "## Root cause", "## Ruled out", "## Timeline (UTC)", "## Follow-up actions"):
        assert section in text
    row = isolated["remembered"][0]
    assert row["outcome"] == "fixed_by_action" and row["action"] == {"type": "rollback", "params": {"service": "orders"}}
    assert row["fingerprints"] == ["fp-keyerror"] and row["templates"] == ["checkout failed: <*>"]


def test_failed_mitigation_is_reverted_then_reinvestigated_then_escalated(platform_calls, isolated, monkeypatch):
    policy.autonomy, policy.dry_run = "L2", False
    platform_calls.extend(["not_recovered", "not_recovered"])
    found = incident()
    found.diagnosis = diagnosis("rollback", "orders")
    seen_attempts = []

    async def build_evidence(signals, services):
        return json.loads(json.dumps(EVIDENCE))

    async def no_triage(model, evidence, usage):
        return None

    async def investigate(model, summary, evidence, triage, usage, emit, attempts):
        seen_attempts.append(attempts)
        return diagnosis("restart", "orders")

    monkeypatch.setattr(manager_module, "build_evidence", build_evidence)
    monkeypatch.setattr(manager_module, "run_triage", no_triage)
    monkeypatch.setattr(manager_module.investigator, "investigate", investigate)
    monkeypatch.setattr(manager_module.llm, "triage_model", lambda: None)
    monkeypatch.setattr(manager_module.llm, "investigator_model", lambda: None)

    run(IncidentManager().respond(found))
    assert [a["type"] for a in found.actions] == ["rollback", "restart"]
    assert [a["state"] for a in found.actions] == ["reverted", "not_recovered"]
    assert isolated["reverted"] == ["undo rollback orders"]
    assert "roll back orders to its previous version" in seen_attempts[0][0] and "reverted" in seen_attempts[0][0]
    assert found.status == "escalated" and found.live  # keeps collecting signals until they clear
    assert "2 mitigations didn't restore service" in found.timeline[-1]["text"]


def test_a_repeated_failed_action_is_escalated_not_retried(platform_calls, isolated):
    policy.autonomy = "L2"
    found = incident()
    found.actions.append({"type": "rollback", "params": {"service": "orders"}, "state": "reverted"})
    found.diagnosis = diagnosis("rollback", "orders")
    run(IncidentManager().respond(found))
    assert found.status == "escalated" and not isolated["executed"]


def test_l0_only_suggests_and_dry_run_changes_nothing(platform_calls, isolated):
    policy.autonomy = "L0"
    found = incident()
    found.diagnosis = diagnosis("rollback", "orders")
    manager = IncidentManager()
    run(manager.respond(found))
    assert found.pending_action["state"] == "suggested"
    with pytest.raises(ActionError):
        run(manager.approve(found, "miguel"))

    policy.autonomy, policy.dry_run = "L2", True
    other = incident(42)
    other.diagnosis = diagnosis("rollback", "orders")
    run(manager.respond(other))
    assert other.actions[0]["state"] == "simulated" and not isolated["executed"]


@pytest.mark.parametrize("action", ["escalate", "open_fix_pr"])
def test_no_runnable_action_means_escalation(action, isolated):
    found = incident()
    found.diagnosis = diagnosis(action, "orders")
    run(IncidentManager().respond(found))
    assert found.status == "escalated" and not found.actions


def test_rejection_escalates(platform_calls, isolated):
    policy.autonomy = "L1"
    found = incident()
    found.diagnosis = diagnosis("rollback", "orders")
    manager = IncidentManager()
    run(manager.respond(found))
    run(manager.reject(found, "miguel", "deploy freeze"))
    assert found.actions[0]["state"] == "rejected" and found.status == "escalated"


def test_human_chosen_action_runs_through_the_same_loop(platform_calls, isolated):
    platform_calls.append("recovered")
    found = incident()
    found.status = "escalated"

    async def scenario():
        manager = IncidentManager()
        with pytest.raises(ActionError, match="not allowed"):
            await manager.run_action(found, "rollback", {"service": "postgres"}, "miguel")
        await manager.run_action(found, "restart", {"service": "orders"}, "miguel")
        await settle(manager)

    run(scenario())
    assert isolated["executed"] == [("restart", {"service": "orders"})]
    assert found.status == "resolved" and found.resolved_at is not None
    assert isolated["remembered"][0]["outcome"] == "fixed_by_action"


# ------------------------------------------------------------------ memory
def test_memory_ignores_zero_confidence_diagnoses():
    found = incident()
    found.diagnosis = diagnosis("escalate", "orders", confidence=0.0)
    found.resolved_at = utcnow()
    row = memory.record(found)
    assert row["category"] is None and row["summary"] is None
    assert row["service"] == "orders" and row["outcome"] == "recovered_without_action"


def test_seen_before_wording():
    match = {"incident_id": 12, "service": "orders", "category": "bad_deploy_code_bug", "outcome": "fixed_by_action",
             "action": {"type": "rollback", "params": {"service": "orders"}}}
    assert memory.describe(match) == "incident #12 (orders, bad_deploy_code_bug), fixed by rollback"


def test_evidence_refresh_flags_seen_before(isolated, monkeypatch):
    match = {"incident_id": 12, "closed_at": utcnow(), "title": "error rate on orders", "service": "orders",
             "category": "bad_deploy_code_bug", "summary": "KeyError", "outcome": "fixed_by_action",
             "action": {"type": "rollback", "params": {"service": "orders"}}, "resolve_s": 190.0,
             "shared_fingerprints": 1, "shared_templates": 0}

    async def recall(exclude, fingerprints, templates, limit):
        assert fingerprints == ["fp-keyerror"]
        return [match]

    async def build_evidence(signals, services):
        return json.loads(json.dumps(EVIDENCE))

    monkeypatch.setattr(db, "recall", recall)
    monkeypatch.setattr(manager_module, "build_evidence", build_evidence)
    found = incident()
    evidence = run(IncidentManager().refresh_evidence(found))
    assert evidence["past_incidents"][0]["incident_id"] == 12
    assert found.timeline[-1]["text"] == "Seen before: incident #12 (orders, bad_deploy_code_bug), fixed by rollback"
