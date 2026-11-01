"""Mitigations Sherlog can run, all through the deploy platform.

Only whitelisted actions on whitelisted services get past `check`. Before an
action runs, `execute` records how to undo it, so a mitigation that doesn't
restore the service can be reverted with `revert`.
"""

from sherlog.config import SERVICES
from sherlog.ingest.clients import platform

DATASTORES = ("redis", "postgres")

# Action -> services it may touch (None: not tied to a service).
ALLOWED: dict[str, tuple[str, ...] | None] = {
    "rollback": SERVICES,
    "config_revert": SERVICES,
    "restart": SERVICES + DATASTORES,
    "scale": SERVICES,
    "flag_off": None,
}
RISKS = ("low", "medium", "high")


def check(type_: str, params: dict) -> str | None:
    """Why this action can't run, or None if it can."""
    if type_ not in ALLOWED:
        return f"{type_} is not an action Sherlog can run"
    if type_ == "flag_off":
        return None if params.get("flag") else "flag_off needs params.flag"
    service = params.get("service")
    if service not in ALLOWED[type_]:
        return f"{type_} is not allowed on {service or 'no service'}"
    if type_ == "scale" and not 1 <= (params.get("replicas") or 0) <= 4:
        return "scale needs params.replicas between 1 and 4"
    return None


def risk(type_: str, params: dict, stated: str) -> str:
    """The model's risk, raised to the policy's floor for the action."""
    floor = "medium" if type_ == "restart" and params.get("service") in DATASTORES else "low"
    return max(stated, floor, key=RISKS.index)


def describe(type_: str, params: dict) -> str:
    service = params.get("service")
    return {
        "rollback": f"roll back {service} to its previous version",
        "config_revert": f"revert the last config change on {service}",
        "restart": f"restart {service}",
        "scale": f"scale {service} to {params.get('replicas')} replicas",
        "flag_off": f"turn flag {params.get('flag')} off",
    }.get(type_, type_)


async def execute(type_: str, params: dict) -> tuple[dict, dict | None]:
    """Run the action. Returns the platform's answer and how to undo it (None if it can't be undone)."""
    service = params.get("service")
    if type_ == "rollback":
        current = (await platform.history(service))[-1]
        result = await platform.act("POST", "/rollback", {"service": service})
        undo = _undo(f"redeploy {service} {current['version']}", "POST", "/deploy",
                     {"service": service, "ref": current["commit_sha"]})
    elif type_ == "config_revert":
        result = await platform.act("POST", f"/config/{service}/revert", {})
        restore = {k: c["from"] for k, c in result["changes"].items() if c["from"] not in (None, "***")}
        undo = _undo(f"reapply the reverted config on {service}", "PUT", f"/config/{service}", {"env": restore}) if restore else None
    elif type_ == "flag_off":
        flag = params["flag"]
        was_on = bool((await platform.flags()).get(flag))
        result = await platform.act("PUT", f"/flags/{flag}", {"enabled": False})
        undo = _undo(f"turn flag {flag} back on", "PUT", f"/flags/{flag}", {"enabled": True}) if was_on else None
    elif type_ == "scale":
        before = next(s["replicas"] for s in await platform.services() if s["service"] == service)
        result = await platform.act("POST", "/scale", {"service": service, "replicas": params["replicas"]})
        undo = _undo(f"scale {service} back to {before}", "POST", "/scale", {"service": service, "replicas": before})
    elif type_ == "restart":
        result = await platform.act("POST", "/restart", {"service": service})
        undo = None
    else:
        raise ValueError(check(type_, params) or f"no executor for {type_}")
    return result, undo


async def revert(undo: dict) -> dict:
    return await platform.act(undo["method"], undo["path"], undo["body"])


def _undo(description: str, method: str, path: str, body: dict) -> dict:
    return {"description": description, "method": method, "path": path, "body": body}
