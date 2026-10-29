"""Container lifecycle for demo services, through the Docker socket.

Services start under docker compose. The platform adopts those containers by
snapshotting how compose runs them (env, networks, limits, ports) and then
replaces them with new images or env while keeping compose's names and labels.
"""

import io
import logging
import os
import secrets
import time
from collections.abc import Callable

import docker
from docker.errors import APIError, NotFound

PROJECT = os.environ.get("COMPOSE_PROJECT", "sherlog")
HEALTH_TIMEOUT_S = 90

log = logging.getLogger("platform.docker")
client = docker.from_env()


class DeployError(Exception):
    pass


def containers_for(service: str) -> list:
    found = client.containers.list(
        all=True,
        filters={"label": [f"com.docker.compose.project={PROJECT}", f"com.docker.compose.service={service}"]},
    )
    return sorted(found, key=lambda c: int(c.labels.get("com.docker.compose.container-number", "1")))


def snapshot(service: str) -> dict:
    """Capture the run spec of a service's first container."""
    current = containers_for(service)
    if not current:
        raise LookupError(f"no containers for {service}")
    attrs = current[0].attrs
    image_ref = attrs["Config"]["Image"]
    image_env = set(client.images.get(image_ref).attrs["Config"].get("Env") or [])
    env = dict(item.split("=", 1) for item in attrs["Config"].get("Env") or [] if item not in image_env)
    host = attrs["HostConfig"]
    return {
        "image": image_ref,
        "env": env,
        "labels": dict(attrs["Config"].get("Labels") or {}),
        "networks": list(attrs["NetworkSettings"]["Networks"].keys()),
        "mem_limit": host.get("Memory") or 0,
        "nano_cpus": host.get("NanoCpus") or 0,
        "restart_policy": host.get("RestartPolicy") or {"Name": "unless-stopped"},
        "port_bindings": host.get("PortBindings") or {},
    }


def ensure_image(service: str, sha: str, context: Callable[[], bytes]) -> str:
    """Build sherlog-demo/<service>:<sha7> from a git archive unless it exists."""
    tag = f"sherlog-demo/{service}:{sha[:7]}"
    try:
        client.images.get(tag)
        return tag
    except NotFound:
        pass
    log.info("Building %s", tag)
    started = time.monotonic()
    client.images.build(
        fileobj=io.BytesIO(context()),
        custom_context=True,
        tag=tag,
        buildargs={"SERVICE": service, "VERSION": sha[:7]},
        labels={"sherlog.commit": sha},
        rm=True,
    )
    log.info("Built %s in %.1fs", tag, time.monotonic() - started)
    return tag


def _create(service: str, spec: dict, image: str, env: dict, number: int, name: str, publish: bool):
    api = client.api
    port_bindings, exposed = None, None
    if publish and spec["port_bindings"]:
        port_bindings, exposed = {}, []
        for container_port, bindings in spec["port_bindings"].items():
            port, proto = container_port.split("/")
            exposed.append((int(port), proto))
            port_bindings[container_port] = [(b.get("HostIp") or "", b["HostPort"]) for b in bindings]
    policy = spec["restart_policy"]
    host_config = api.create_host_config(
        mem_limit=spec["mem_limit"] or None,
        nano_cpus=spec["nano_cpus"] or None,
        restart_policy={"Name": policy.get("Name") or "no", "MaximumRetryCount": policy.get("MaximumRetryCount", 0)},
        port_bindings=port_bindings,
    )
    first_network, *other_networks = spec["networks"]
    networking = api.create_networking_config({first_network: api.create_endpoint_config(aliases=[service])})
    labels = {
        **spec["labels"],
        "com.docker.compose.container-number": str(number),
        "sherlog.platform/managed": "true",
    }
    created = api.create_container(
        image=image,
        name=name,
        environment=env,
        labels=labels,
        host_config=host_config,
        networking_config=networking,
        ports=exposed,
    )
    container = client.containers.get(created["Id"])
    for network in other_networks:
        client.networks.get(network).connect(container, aliases=[service])
    container.start()
    return container


def _remove(container) -> None:
    try:
        container.reload()
        if container.status == "paused":
            container.unpause()
        container.stop(timeout=10)
    except (APIError, NotFound):
        pass
    try:
        container.remove(force=True)
    except NotFound:
        pass


def wait_healthy(container, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs["State"]
        if state["Status"] in ("exited", "dead"):
            return False
        health = (state.get("Health") or {}).get("Status")
        if health == "healthy" or (health is None and state["Status"] == "running"):
            return True
        if health == "unhealthy":
            return False
        time.sleep(1)
    return False


def roll(service: str, spec: dict, image: str, env: dict, replicas: int) -> None:
    """Replace every replica with (image, env), one at a time.

    New replicas must pass their health check before the old one is removed.
    A replica that publishes host ports is stopped first, since the port can't
    be bound twice.
    """
    existing = {c.name: c for c in containers_for(service)}
    for number in range(1, replicas + 1):
        name = f"{PROJECT}-{service}-{number}"
        old = existing.get(name)
        publish = number == 1 and bool(spec["port_bindings"])
        if publish:
            if old:
                _remove(old)
            new = _create(service, spec, image, env, number, name, publish=True)
            if not wait_healthy(new):
                raise DeployError(f"{name} failed health checks after deploy")
            continue
        new = _create(service, spec, image, env, number, f"{name}-next-{secrets.token_hex(3)}", publish=False)
        if not wait_healthy(new):
            tail = new.logs(tail=15).decode(errors="replace")
            _remove(new)
            raise DeployError(f"{name}: new container failed health checks; old one kept.\n{tail}")
        if old:
            _remove(old)
        new.rename(name)

    for container in containers_for(service):
        if int(container.labels.get("com.docker.compose.container-number", "1")) > replicas:
            _remove(container)


def restart(service: str) -> None:
    targets = containers_for(service)
    if not targets:
        raise DeployError(f"no containers for {service}")
    for container in targets:
        container.reload()
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status == "running":
            container.restart(timeout=5)
        else:
            container.start()
    for container in targets:
        if not wait_healthy(container):
            raise DeployError(f"{container.name} not healthy after restart")


def status(service: str) -> list[dict]:
    result = []
    for container in containers_for(service):
        state = container.attrs["State"]
        status_, health = state["Status"], (state.get("Health") or {}).get("Status")
        if status_ == "paused":
            # A frozen process looks like a hung one to a platform: running, failing readiness.
            status_, health = "running", "unhealthy"
        result.append(
            {
                "name": container.name,
                "status": status_,
                "health": health,
                "image": container.attrs["Config"]["Image"],
                "started_at": state.get("StartedAt"),
                "restart_count": container.attrs.get("RestartCount", 0),
                "oom_killed": state.get("OOMKilled", False),
                "exit_code": state.get("ExitCode"),
            }
        )
    return result
