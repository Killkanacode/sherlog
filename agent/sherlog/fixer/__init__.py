"""Code fixes: when a deploy shipped a bug, Muse Code writes the fix and a
test in the fixer service, which checks them and opens a pull request.

Sherlog decides when (a bad_deploy_code_bug diagnosis with a deploy to
blame), gathers what the fixer needs (the deploy's diff, the code as deployed
and the service's exceptions) and follows the job. The rollback stays the
mitigation; the pull request is the lasting fix, for a human to review.
"""

import base64

import httpx

from sherlog.config import settings

FINAL_STATES = ("done", "failed")


class FixerError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.fixer_url) and not settings.fake_llm


def blamed_deploy(evidence: dict | None, diagnosis: dict | None) -> dict | None:
    """The deploy behind a code bug: the latest forward deploy of the root-cause
    service in the evidence, preferring one the diagnosis cites."""
    cause = (diagnosis or {}).get("root_cause") or {}
    if cause.get("category") != "bad_deploy_code_bug":
        return None
    deploys = [
        c for c in (evidence or {}).get("changes", [])
        if c.get("type") == "deploy" and c.get("service") == cause.get("service") and c.get("commit_sha")
        and c.get("from_version") and not (c.get("details") or {}).get("rollback")
    ]
    if not deploys:
        return None
    cited = {e.get("ref") for e in (diagnosis or {}).get("evidence", [])}
    return max(deploys, key=lambda c: (c.get("id") in cited, c.get("timestamp", "")))


def evidence_text(evidence: dict | None, service: str, limit: int = 3) -> str:
    """The service's busiest exceptions, as the fixer's prompt shows them."""
    exceptions = [e for e in (evidence or {}).get("exceptions", []) if e.get("service") == service]
    exceptions.sort(key=lambda e: e.get("per_minute") or 0, reverse=True)
    blocks = [
        f"{e.get('type')}: {e.get('message')} ({e.get('per_minute')}/min on version {e.get('version')})\n{e.get('stack', '')}"
        for e in exceptions[:limit]
    ]
    return "\n\n".join(blocks)


class Client:
    def __init__(self, base_url: str = settings.fixer_url) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    async def submit(self, incident_id: int, deploy: dict, diff: str, source: bytes, root_cause: dict, evidence: str) -> dict:
        response = await self._http.post(
            "/jobs",
            json={
                "incident_id": incident_id,
                "service": deploy["service"],
                "version": deploy["to_version"],
                "commit_sha": deploy["commit_sha"],
                "commit_message": deploy.get("commit_message") or "",
                "diff": diff,
                "root_cause": root_cause,
                "evidence": evidence,
                "source": base64.b64encode(source).decode(),
            },
        )
        if response.is_error:
            raise FixerError(f"fixer refused the job ({response.status_code}): {response.text[:200]}")
        return response.json()

    async def job(self, job_id: str) -> dict:
        response = await self._http.get(f"/jobs/{job_id}")
        response.raise_for_status()
        return response.json()


client = Client()
