import dataclasses

import pytest

from sherlog import fixer
from sherlog.incidents import manager as manager_module
from sherlog.incidents.manager import IncidentManager
from tests.test_actions import diagnosis, incident, isolated, run  # noqa: F401 - isolated is an autouse fixture

DEPLOY = {"id": "evt_1", "type": "deploy", "service": "orders", "from_version": "main", "to_version": "25b87ad",
          "commit_sha": "25b87ad0049b74c84a218a5c424ede4705ead826", "commit_message": "orders: simplify order total helper",
          "timestamp": "2026-11-04T18:01:43+00:00", "details": {"rollback": False}}


def test_blamed_deploy_is_the_root_cause_services_forward_deploy():
    rollback = {**DEPLOY, "id": "evt_2", "timestamp": "2026-11-04T18:03:00+00:00", "details": {"rollback": True}}
    other = {**DEPLOY, "id": "evt_3", "service": "payments"}
    evidence = {"changes": [DEPLOY, rollback, other]}
    assert fixer.blamed_deploy(evidence, diagnosis("rollback", "orders"))["id"] == "evt_1"
    not_code = diagnosis("restart", "orders")
    not_code["root_cause"]["category"] = "process_crash"
    assert fixer.blamed_deploy(evidence, not_code) is None
    assert fixer.blamed_deploy({"changes": [{**DEPLOY, "commit_sha": None}]}, diagnosis("rollback", "orders")) is None


def test_evidence_text_keeps_the_services_busiest_exceptions():
    evidence = {"exceptions": [
        {"service": "orders", "type": "KeyError", "message": "'price'", "per_minute": 250, "version": "25b87ad", "stack": "Traceback"},
        {"service": "orders", "type": "TimeoutError", "message": "pool", "per_minute": 3, "version": "25b87ad"},
        {"service": "payments", "type": "ValueError", "message": "x", "per_minute": 900},
    ]}
    text = fixer.evidence_text(evidence, "orders", limit=1)
    assert text.startswith("KeyError: 'price' (250/min") and "payments" not in text and "TimeoutError" not in text


class FakeFixer:
    def __init__(self, final: dict):
        self.final, self.polls, self.submitted = final, 0, None

    async def submit(self, incident_id, deploy, diff, source, root_cause, evidence):
        self.submitted = {"incident_id": incident_id, "version": deploy["to_version"], "diff": diff, "source": source}
        return {"id": "fix_1", "state": "queued", "steps": []}

    async def job(self, job_id):
        self.polls += 1
        if self.polls == 1:
            return {"id": job_id, "state": "writing", "steps": [{"text": "Muse Code is writing the fix"}]}
        return {"id": job_id, "steps": [{"text": "Muse Code is writing the fix"}, {"text": "Verified"}], **self.final}


@pytest.fixture
def fixer_env(monkeypatch):
    async def no_sleep(_):
        return None

    async def diff(service, a, b):
        return "--- a/orders/pricing.py\n+++ b/orders/pricing.py"

    async def archive(service, version):
        return b"tarball"

    monkeypatch.setattr(manager_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(manager_module.platform, "diff", diff)
    monkeypatch.setattr(manager_module.platform, "archive", archive)
    monkeypatch.setattr(manager_module, "settings", dataclasses.replace(
        manager_module.settings, price_investigator_in=1.25, price_investigator_out=4.25, price_investigator_cached=0.15))


def prepared():
    found = incident()
    found.evidence["changes"] = [DEPLOY]
    found.diagnosis = diagnosis("rollback", "orders")
    found.postmortem = "# Incident 41\n"
    return found


def test_a_verified_fix_lands_in_the_timeline_usage_and_postmortem(monkeypatch, fixer_env):
    fake = FakeFixer({
        "state": "done", "seconds": 120.0, "verified": {"passes_on_fix": True, "fails_on_deployed": True},
        "pr": {"url": "https://github.com/acme/shop/pull/7", "number": 7},
        "usage": {"model": "muse-spark-1.3", "calls": 9, "input_tokens": 200_000, "output_tokens": 4_000, "cached_tokens": 150_000},
    })
    monkeypatch.setattr(fixer, "client", fake)
    found = prepared()
    run(IncidentManager().write_fix(found))
    assert fake.submitted == {"incident_id": 41, "version": "25b87ad", "diff": "--- a/orders/pricing.py\n+++ b/orders/pricing.py",
                              "source": b"tarball"}
    assert found.fix["state"] == "done" and found.fix["pr"]["number"] == 7
    texts = [e["text"] for e in found.timeline if e["kind"] == "fix"]
    assert texts[0].startswith("Muse Code is writing a fix for orders 25b87ad")
    assert "Muse Code is writing the fix" in texts and "Verified" in texts
    assert texts[-1].startswith("Fix PR opened: https://github.com/acme/shop/pull/7")
    call = found.usage.calls[-1]
    assert call["role"] == "fixer" and call["cost_usd"] == pytest.approx((50_000 * 1.25 + 150_000 * 0.15 + 4_000 * 4.25) / 1e6)
    assert "## Code fix" in found.postmortem and "pull/7" in found.postmortem
    assert found.summary()["fix"] == {"state": "done", "pr": found.fix["pr"]}


def test_a_failed_fix_is_reported_and_nothing_else_changes(monkeypatch, fixer_env):
    monkeypatch.setattr(fixer, "client", FakeFixer({"state": "failed", "error": "the new test fails on the fix"}))
    found = prepared()
    run(IncidentManager().write_fix(found))
    assert found.fix["state"] == "failed"
    assert found.timeline[-1]["text"] == "Code fix not proposed: the new test fails on the fix"
    assert found.postmortem == "# Incident 41\n" and not found.usage.calls
