"""Platform API: the shop's (simulated) deployment platform.

Both Sherlog (to mitigate) and the chaos controller (to cause trouble) go
through here. Deploys, config changes and flag flips are emitted as change
events, the way a CI/CD system would report them.
"""

import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from platform_api import docker_ops, gitrepo
from platform_api.docker_ops import DeployError
from platform_api.state import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("platform")

MANAGED = ("gateway", "orders", "payments", "inventory")
DATASTORES = ("redis", "postgres")  # restart only
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
SECRET_KEY_RE = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY", re.I)
URL_CREDENTIALS_RE = re.compile(r"(://[^:/@]+:)[^@]+@")

store = Store(os.environ.get("STATE_PATH", "/data/state.json"))
EVENTS_URL = os.environ.get("SHERLOG_EVENTS_URL", "")
locks = {name: threading.Lock() for name in MANAGED + DATASTORES}


# ------------------------------------------------------------------ helpers
def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask(key: str, value: str) -> str:
    if SECRET_KEY_RE.search(key):
        return "***"
    return URL_CREDENTIALS_RE.sub(r"\1***@", value)


def managed(service: str) -> dict:
    if service not in MANAGED:
        raise HTTPException(status_code=403, detail=f"{service} is not a managed service")
    entry = store.services.get(service)
    if entry is None:
        raise HTTPException(status_code=409, detail=f"{service} has not been adopted yet")
    return entry


def effective_env(entry: dict) -> dict:
    return {**entry["spec"]["env"], **entry["env_overrides"]}


def changed(from_sha: str | None, to_sha: str | None, service: str) -> list[str]:
    if not from_sha or not to_sha or from_sha == to_sha:
        return []
    try:
        return gitrepo.changed_files(from_sha, to_sha, service)
    except gitrepo.GitError:
        return []


def push_event(event: dict) -> None:
    if not EVENTS_URL:
        return
    try:
        httpx.post(EVENTS_URL, json=event, timeout=2.0)
    except httpx.HTTPError as exc:
        log.warning("Could not deliver event %s to Sherlog: %s", event["id"], exc)


def emit(background: BackgroundTasks | None, type_: str, service: str | None, actor: str, **fields) -> dict:
    event = {
        "id": f"evt_{secrets.token_hex(6)}",
        "type": type_,
        "service": service,
        "timestamp": now(),
        "actor": actor,
        **fields,
    }
    store.events.append(event)
    store.save()
    log.info("Change event %s %s %s by %s", event["id"], type_, service or "-", actor)
    if background is not None:
        background.add_task(push_event, event)
    else:
        threading.Thread(target=push_event, args=(event,), daemon=True).start()
    return event


def roll_or_fail(service: str, entry: dict, image: str, env: dict, replicas: int) -> None:
    try:
        docker_ops.roll(service, entry["spec"], image, env, replicas)
    except DeployError as exc:
        log.error("Deploy of %s failed: %s", service, exc)
        raise HTTPException(status_code=502, detail=str(exc))


def adopt_all() -> None:
    """Snapshot compose-run containers the platform doesn't know about yet.

    If the running image differs from what the platform recorded (someone ran
    `docker compose up` behind its back), the service is re-adopted.
    """
    try:
        baseline_sha = gitrepo.resolve("main")
    except gitrepo.GitError:
        baseline_sha = None
    for service in MANAGED:
        try:
            snap = docker_ops.snapshot(service)
        except LookupError:
            log.warning("No running containers for %s; skipping adoption", service)
            continue
        entry = store.services.get(service)
        if entry and entry["history"][-1]["image"] == snap["image"]:
            at_baseline = len(entry["history"]) == 1 and not entry["env_overrides"]
            if at_baseline and snap["env"] != entry["spec"]["env"]:
                log.info("Baseline env of %s changed in compose; updating spec", service)
                entry["spec"] = snap
            continue
        if entry:
            log.warning("Drift detected for %s (running %s); re-adopting", service, snap["image"])
        version = snap["image"].rsplit(":", 1)[-1]
        baseline = {
            "version": version,
            "image": snap["image"],
            "commit_sha": baseline_sha,
            "deployed_at": now(),
            "actor": "compose",
        }
        store.services[service] = {
            "spec": snap,
            "baseline": baseline,
            "history": [baseline],
            "env_overrides": {},
            "config_history": [],
            "replicas": len(docker_ops.containers_for(service)),
        }
        log.info("Adopted %s at version %s", service, version)
    store.save()


@asynccontextmanager
async def lifespan(app: FastAPI):
    adopt_all()
    yield


app = FastAPI(title="platform-api", lifespan=lifespan)


# ------------------------------------------------------------------ models
class ActorRequest(BaseModel):
    actor: str = "unknown"


class ServiceRequest(ActorRequest):
    service: str


class DeployRequest(ServiceRequest):
    ref: str


class ScaleRequest(ServiceRequest):
    replicas: int = Field(ge=1, le=4)


class FlagRequest(ActorRequest):
    enabled: bool


class ConfigRequest(ActorRequest):
    env: dict[str, str | int | float | bool]


# ------------------------------------------------------------------ reads
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/services")
def list_services():
    result = []
    for service in MANAGED:
        entry = store.services.get(service)
        if entry is None:
            continue
        current = entry["history"][-1]
        result.append(
            {
                "service": service,
                "version": current["version"],
                "commit_sha": current["commit_sha"],
                "deployed_at": current["deployed_at"],
                "replicas": entry["replicas"],
                "containers": docker_ops.status(service),
            }
        )
    for service in DATASTORES:
        result.append({"service": service, "containers": docker_ops.status(service)})
    return result


@app.get("/services/{service}/history")
def service_history(service: str):
    entry = managed(service)
    return [{k: v for k, v in record.items() if k != "image"} for record in entry["history"]]


@app.get("/events")
def list_events(since: str | None = None, service: str | None = None, limit: int = Query(100, le=1000)):
    events = store.events
    if since:
        events = [e for e in events if e["timestamp"] >= since]
    if service:
        events = [e for e in events if e["service"] == service]
    return events[-limit:]


def sha_for(service: str, version: str) -> str:
    """Map a deployed version to its commit. Ref names are deliberately not accepted."""
    entry = managed(service)
    for record in [entry["baseline"], *entry["history"]]:
        if record["version"] == version and record["commit_sha"]:
            return record["commit_sha"]
    if SHA_RE.match(version):
        try:
            return gitrepo.resolve(version)
        except gitrepo.GitError:
            pass
    raise HTTPException(status_code=404, detail=f"unknown version {version} for {service}")


@app.get("/source/{service}/{version}/{path:path}", response_class=PlainTextResponse)
def source(service: str, version: str, path: str):
    sha = sha_for(service, version)
    try:
        return gitrepo.show(sha, path)
    except gitrepo.GitError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/diff/{service}", response_class=PlainTextResponse)
def deploy_diff(service: str, from_version: str, to_version: str):
    return gitrepo.diff(sha_for(service, from_version), sha_for(service, to_version), service)


@app.get("/flags")
def get_flags():
    return store.flags


@app.get("/config/{service}")
def get_config(service: str):
    entry = managed(service)
    return {
        "env": {k: mask(k, v) for k, v in sorted(effective_env(entry).items())},
        "overrides": {k: mask(k, v) for k, v in entry["env_overrides"].items()},
    }


# ------------------------------------------------------------------ writes
@app.post("/deploy")
def deploy(req: DeployRequest, background: BackgroundTasks):
    entry = managed(req.service)
    with locks[req.service]:
        try:
            sha = gitrepo.resolve(req.ref)
        except gitrepo.GitError:
            raise HTTPException(status_code=404, detail=f"unknown ref {req.ref}")
        current = entry["history"][-1]
        image = docker_ops.ensure_image(req.service, sha, lambda: gitrepo.archive(sha))
        roll_or_fail(req.service, entry, image, effective_env(entry), entry["replicas"])
        record = {"version": sha[:7], "image": image, "commit_sha": sha, "deployed_at": now(), "actor": req.actor}
        entry["history"].append(record)
        event = emit(
            background,
            "deploy",
            req.service,
            req.actor,
            from_version=current["version"],
            to_version=record["version"],
            commit_sha=sha,
            commit_message=gitrepo.commit_info(sha)["subject"],
            changed_files=changed(current["commit_sha"], sha, req.service),
            details={"rollback": False},
        )
    return {"service": req.service, "version": record["version"], "event_id": event["id"]}


@app.post("/rollback")
def rollback(req: ServiceRequest, background: BackgroundTasks):
    entry = managed(req.service)
    with locks[req.service]:
        if len(entry["history"]) < 2:
            raise HTTPException(status_code=409, detail=f"{req.service} has no previous version")
        current, previous = entry["history"][-1], entry["history"][-2]
        roll_or_fail(req.service, entry, previous["image"], effective_env(entry), entry["replicas"])
        entry["history"].pop()
        event = emit(
            background,
            "deploy",
            req.service,
            req.actor,
            from_version=current["version"],
            to_version=previous["version"],
            commit_sha=previous["commit_sha"],
            commit_message=f"Rollback to {previous['version']}",
            changed_files=changed(current["commit_sha"], previous["commit_sha"], req.service),
            details={"rollback": True},
        )
    return {"service": req.service, "version": previous["version"], "event_id": event["id"]}


@app.post("/restart")
def restart(req: ServiceRequest):
    if req.service not in MANAGED + DATASTORES:
        raise HTTPException(status_code=403, detail=f"{req.service} cannot be restarted by the platform")
    with locks[req.service]:
        try:
            docker_ops.restart(req.service)
        except DeployError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
    log.info("Restarted %s (actor %s)", req.service, req.actor)
    return {"service": req.service, "restarted": True}


@app.post("/scale")
def scale(req: ScaleRequest):
    entry = managed(req.service)
    with locks[req.service]:
        current = entry["history"][-1]
        roll_or_fail(req.service, entry, current["image"], effective_env(entry), req.replicas)
        entry["replicas"] = req.replicas
        store.save()
    log.info("Scaled %s to %d (actor %s)", req.service, req.replicas, req.actor)
    return {"service": req.service, "replicas": req.replicas}


def env_changes(before: dict, after: dict) -> dict:
    return {
        key: {"from": mask(key, before[key]) if key in before else None, "to": mask(key, after[key]) if key in after else None}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }


def apply_config(service: str, entry: dict, overrides: dict, actor: str, background, revert: bool) -> dict:
    before = effective_env(entry)
    previous_overrides = dict(entry["env_overrides"])
    entry["env_overrides"] = overrides
    after = effective_env(entry)
    current = entry["history"][-1]
    try:
        docker_ops.roll(service, entry["spec"], current["image"], after, entry["replicas"])
    except DeployError as exc:
        entry["env_overrides"] = previous_overrides
        raise HTTPException(status_code=502, detail=str(exc))
    changes = env_changes(before, after)
    return emit(
        background,
        "config",
        service,
        actor,
        from_version=current["version"],
        to_version=current["version"],
        details={"changes": changes, "revert": revert},
    )


@app.put("/config/{service}")
def set_config(service: str, req: ConfigRequest, background: BackgroundTasks):
    entry = managed(service)
    with locks[service]:
        entry["config_history"].append(dict(entry["env_overrides"]))
        overrides = {**entry["env_overrides"], **{k: str(v) for k, v in req.env.items()}}
        event = apply_config(service, entry, overrides, req.actor, background, revert=False)
    return {"service": service, "event_id": event["id"], "changes": event["details"]["changes"]}


@app.post("/config/{service}/revert")
def revert_config(service: str, req: ActorRequest, background: BackgroundTasks):
    entry = managed(service)
    with locks[service]:
        if not entry["config_history"]:
            raise HTTPException(status_code=409, detail=f"{service} has no config change to revert")
        previous = entry["config_history"].pop()
        event = apply_config(service, entry, previous, req.actor, background, revert=True)
    return {"service": service, "event_id": event["id"], "changes": event["details"]["changes"]}


@app.put("/flags/{name}")
def set_flag(name: str, req: FlagRequest, background: BackgroundTasks):
    before = bool(store.flags.get(name, False))
    store.flags[name] = req.enabled
    store.save()
    if before == req.enabled:
        return {"flag": name, "enabled": req.enabled, "changed": False}
    event = emit(background, "flag", None, req.actor, details={"flag": name, "from": before, "to": req.enabled})
    return {"flag": name, "enabled": req.enabled, "changed": True, "event_id": event["id"]}


@app.post("/baseline")
def restore_baseline(req: ActorRequest, background: BackgroundTasks):
    """Return every service to its adopted baseline and switch all flags off."""
    restored = []
    for service in MANAGED:
        entry = store.services.get(service)
        if entry is None:
            continue
        with locks[service]:
            current, baseline = entry["history"][-1], entry["baseline"]
            running = all(c["status"] == "running" for c in docker_ops.status(service))
            dirty = (
                current["image"] != baseline["image"]
                or entry["env_overrides"]
                or entry["replicas"] != 1
                or not running
            )
            if not dirty:
                continue
            env_before = effective_env(entry)
            roll_or_fail(service, entry, baseline["image"], entry["spec"]["env"], 1)
            entry.update(env_overrides={}, config_history=[], replicas=1, history=[baseline])
            reverted = env_changes(env_before, effective_env(entry))
            if current["image"] != baseline["image"]:
                emit(
                    background,
                    "deploy",
                    service,
                    req.actor,
                    from_version=current["version"],
                    to_version=baseline["version"],
                    commit_sha=baseline["commit_sha"],
                    commit_message=f"Restore {baseline['version']}",
                    changed_files=changed(current["commit_sha"], baseline["commit_sha"], service),
                    details={"rollback": True},
                )
            if reverted:
                emit(
                    background,
                    "config",
                    service,
                    req.actor,
                    from_version=baseline["version"],
                    to_version=baseline["version"],
                    details={"changes": reverted, "revert": True},
                )
            restored.append(service)
    for name, enabled in list(store.flags.items()):
        if enabled:
            store.flags[name] = False
            emit(background, "flag", None, req.actor, details={"flag": name, "from": True, "to": False})
    store.save()
    return {"restored": restored, "flags": store.flags}
