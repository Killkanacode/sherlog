"""Fault injection mechanisms.

Each injector returns an `undo` dict that its reverter understands. Platform
changes (deploys, config, flags) go through platform-api so they look exactly
like changes made by an engineer; process and network faults go straight to
Docker and Toxiproxy, which Sherlog can only observe through telemetry.
"""

import os
import random

import docker
import httpx
from docker.errors import APIError, NotFound

PROJECT = os.environ.get("COMPOSE_PROJECT", "sherlog")
PLATFORM_URL = os.environ.get("PLATFORM_API_URL", "http://platform-api:8050")
TOXIPROXY_URL = os.environ.get("TOXIPROXY_URL", "http://toxiproxy:8474")

SERVICES = ("gateway", "orders", "payments", "inventory")
DATASTORES = ("redis", "postgres")
PROXIES = ("orders_to_inventory", "orders_to_payments")

# Who appears as the author of injected platform changes.
ACTORS = ("maria.lopez", "dev.kumar", "j.chen", "ci-bot")

TYPES = {
    "kill": {"target": "service", "description": "SIGKILL a service's containers (process crash)"},
    "pause": {"target": "service", "description": "Freeze a service's containers (silent hang)"},
    "stop": {"target": "service", "description": "Stop a container, e.g. a datastore going down"},
    "latency": {
        "target": "proxy",
        "description": "Add latency to a service-to-service link",
        "params": {"latency_ms": 2000, "jitter_ms": 100},
    },
    "partition": {"target": "proxy", "description": "Cut a service-to-service link"},
    "deploy": {"target": "service", "description": "Deploy a git ref through the platform", "params": {"ref": "main"}},
    "config": {"target": "service", "description": "Change service env through the platform", "params": {"env": {}}},
    "flag": {"target": "flag", "description": "Flip a feature flag through the platform", "params": {"enabled": True}},
}

docker_client = docker.from_env()
http = httpx.Client(timeout=180.0)


class FaultError(Exception):
    pass


def _containers(service: str) -> list:
    if service not in SERVICES + DATASTORES:
        raise FaultError(f"unknown service {service}")
    found = docker_client.containers.list(
        all=True,
        filters={"label": [f"com.docker.compose.project={PROJECT}", f"com.docker.compose.service={service}"]},
    )
    if not found:
        raise FaultError(f"no containers for {service}")
    return found


def _platform(method: str, path: str, **kwargs) -> dict:
    response = http.request(method, f"{PLATFORM_URL}{path}", **kwargs)
    if response.status_code >= 400:
        raise FaultError(f"platform-api {method} {path}: {response.status_code} {response.text[:300]}")
    return response.json()


def _toxiproxy(method: str, path: str, **kwargs) -> None:
    response = http.request(method, f"{TOXIPROXY_URL}{path}", **kwargs)
    if response.status_code >= 400 and response.status_code != 404:
        raise FaultError(f"toxiproxy {method} {path}: {response.status_code} {response.text[:300]}")


def _proxy(name: str) -> str:
    if name not in PROXIES:
        raise FaultError(f"unknown proxy {name}; expected one of {PROXIES}")
    return name


def inject(fault_id: str, type_: str, target: str, params: dict) -> dict:
    actor = params.get("actor") or random.choice(ACTORS)

    if type_ == "kill":
        for container in _containers(target):
            container.kill()
        return {}
    if type_ == "pause":
        for container in _containers(target):
            container.pause()
        return {}
    if type_ == "stop":
        for container in _containers(target):
            container.stop(timeout=5)
        return {}
    if type_ == "latency":
        toxic = f"chaos_{fault_id}"
        _toxiproxy(
            "POST",
            f"/proxies/{_proxy(target)}/toxics",
            json={
                "name": toxic,
                "type": "latency",
                "stream": "downstream",
                "toxicity": 1.0,
                "attributes": {"latency": int(params.get("latency_ms", 2000)), "jitter": int(params.get("jitter_ms", 0))},
            },
        )
        return {"toxic": toxic}
    if type_ == "partition":
        _toxiproxy("POST", f"/proxies/{_proxy(target)}", json={"enabled": False})
        return {}
    if type_ == "deploy":
        result = _platform("POST", "/deploy", json={"service": target, "ref": params["ref"], "actor": actor})
        return {"version": result["version"]}
    if type_ == "config":
        env = {k: str(v) for k, v in params["env"].items()}
        _platform("PUT", f"/config/{target}", json={"env": env, "actor": actor})
        return {"env": env}
    if type_ == "flag":
        before = _platform("GET", "/flags").get(target, False)
        _platform("PUT", f"/flags/{target}", json={"enabled": bool(params.get("enabled", True)), "actor": actor})
        return {"previous": before}
    raise FaultError(f"unknown fault type {type_}")


def revert(fault: dict) -> None:
    """Undo one fault. Safe to call when Sherlog (or a human) already fixed it."""
    type_, target, undo = fault["type"], fault["target"], fault["undo"]

    if type_ in ("kill", "stop"):
        for container in _containers(target):
            container.reload()
            if container.status != "running":
                container.start()
    elif type_ == "pause":
        for container in _containers(target):
            container.reload()
            if container.status == "paused":
                container.unpause()
    elif type_ == "latency":
        _toxiproxy("DELETE", f"/proxies/{target}/toxics/{undo['toxic']}")
    elif type_ == "partition":
        _toxiproxy("POST", f"/proxies/{target}", json={"enabled": True})
    elif type_ == "deploy":
        current = next(s for s in _platform("GET", "/services") if s["service"] == target)
        if current["version"] == undo["version"]:
            _platform("POST", "/rollback", json={"service": target, "actor": "ops"})
    elif type_ == "config":
        overrides = _platform("GET", f"/config/{target}")["overrides"]
        if all(overrides.get(k) == v for k, v in undo["env"].items()):
            _platform("POST", f"/config/{target}/revert", json={"actor": "ops"})
    elif type_ == "flag":
        _platform("PUT", f"/flags/{target}", json={"enabled": undo["previous"], "actor": "ops"})


def restore_baseline() -> dict:
    """Bring everything back to normal: network, processes, deploys, config, flags."""
    _toxiproxy("POST", "/reset")
    started = []
    for service in SERVICES + DATASTORES:
        for container in _containers(service):
            container.reload()
            try:
                if container.status == "paused":
                    container.unpause()
                    started.append(container.name)
                elif container.status != "running":
                    container.start()
                    started.append(container.name)
            except (APIError, NotFound) as exc:
                raise FaultError(f"could not restore {container.name}: {exc}")
    platform = _platform("POST", "/baseline", json={"actor": "ops"})
    return {"containers_started": started, "platform": platform}
