"""Incident memory: what past incidents looked like and what fixed them.

When an incident closes, its exception fingerprints, key log templates, root
cause, outcome and the action that restored service are stored. A new
incident whose evidence shares a fingerprint or a template is flagged "seen
before", and the investigator can search memory with search_past_incidents.
A diagnosis with zero confidence (fake models, or a fallback) is not stored
as knowledge.
"""

from sherlog import db
from sherlog.detectors.logs import log_detector


def keys(evidence: dict) -> tuple[list[str], list[str]]:
    """Exception fingerprints and WARN/ERROR templates of an evidence pack."""
    fingerprints = sorted({e["fingerprint"] for e in evidence.get("exceptions", [])})
    templates = sorted({t["template"] for t in evidence.get("log_templates", []) if t["level"] in ("WARN", "ERROR")})
    return fingerprints, templates


def record(incident) -> dict:
    evidence = incident.evidence or {}
    fingerprints, templates = keys(evidence)
    cause = (incident.diagnosis or {}).get("root_cause") or {}
    known = cause.get("confidence", 0) > 0
    fixed = next((a for a in incident.actions if a["state"] == "recovered"), None)
    if fixed:
        outcome = "fixed_by_action"
    elif incident.status == "escalated":
        outcome = "escalated"
    else:
        outcome = "recovered_without_action"
    return {
        "incident_id": incident.id,
        "closed_at": incident.resolved_at,
        "title": incident.title,
        "service": cause["service"] if known else (evidence.get("root_cause_hint") or {}).get("service"),
        "category": cause["category"] if known else None,
        "summary": cause["summary"] if known else None,
        "outcome": outcome,
        "action": {"type": fixed["type"], "params": fixed["params"]} if fixed else None,
        "fingerprints": fingerprints,
        "templates": templates,
        "resolve_s": round((incident.resolved_at - incident.created_at).total_seconds(), 1),
    }


async def remember(incident) -> dict:
    row = record(incident)
    await db.remember(row)
    return row


async def recall(incident_id: int, evidence: dict) -> list[dict]:
    fingerprints, templates = keys(evidence)
    if not (fingerprints or templates):
        return []
    rows = await db.recall(incident_id, fingerprints, templates, limit=3)
    return [_public(row) for row in rows]


async def search(
    incident_id: int, fingerprint: str | None = None, template: str | None = None, service: str | None = None
) -> list[dict]:
    known = log_detector.templates.get(template) if template else None  # a template id from the evidence
    rows = await db.recall(
        incident_id,
        fingerprints=[fingerprint] if fingerprint else [],
        template_like=known.text if known else template,
        service=service,
    )
    return [_public(row) for row in rows]


def describe(match: dict) -> str:
    """'incident #12 (orders, bad_deploy_code_bug), fixed by rollback'."""
    what = ", ".join(x for x in (match.get("service"), match.get("category")) if x)
    outcome = {
        "fixed_by_action": f"fixed by {(match.get('action') or {}).get('type')}",
        "escalated": "escalated to a human",
        "recovered_without_action": "recovered without action",
    }[match["outcome"]]
    return f"incident #{match['incident_id']}" + (f" ({what})" if what else "") + f", {outcome}"


def _public(row: dict) -> dict:
    return {
        "incident_id": row["incident_id"],
        "closed_at": row["closed_at"].isoformat(),
        "title": row["title"],
        "service": row["service"],
        "category": row["category"],
        "summary": row["summary"],
        "outcome": row["outcome"],
        "action": row["action"],
        "resolve_s": row["resolve_s"],
        "shared_fingerprints": row.get("shared_fingerprints", 0),
        "shared_templates": row.get("shared_templates", 0),
    }
