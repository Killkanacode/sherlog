"""Container lifecycle signals from the platform API: restarts, OOM kills,
exits and failing readiness. Sherlog gets these from the platform (like an SRE
reading pod status), never from the Docker socket."""

import asyncio
import logging

from sherlog.ingest.clients import platform
from sherlog.signals import bus

log = logging.getLogger("sherlog.detectors.containers")

POLL_S = 5
CONFIRM_POLLS = 2


class ContainerDetector:
    def __init__(self) -> None:
        self.restarts: dict[str, int] = {}
        self.started_at: dict[str, str] = {}
        self.down_polls: dict[str, int] = {}
        self.unready_polls: dict[str, int] = {}
        self.last: list[dict] = []

    def _observe(self, services: list[dict]) -> None:
        for entry in services:
            service = entry["service"]
            containers = entry["containers"]
            for c in containers:
                name = c["name"]
                previous = self.restarts.get(name)
                if previous is not None and c["restart_count"] > previous:
                    bus.event(
                        "container_restart",
                        service,
                        "critical",
                        {
                            "container": name,
                            "restart_count": c["restart_count"],
                            "oom_killed": c["oom_killed"],
                            "exit_code": c["exit_code"],
                        },
                    )
                self.restarts[name] = c["restart_count"]

            down = not containers or all(c["status"] not in ("running", "restarting") for c in containers)
            self.down_polls[service] = self.down_polls.get(service, 0) + 1 if down else 0
            if self.down_polls[service] >= CONFIRM_POLLS:
                exit_codes = sorted({c["exit_code"] for c in containers})
                bus.fire(
                    f"down:{service}",
                    "container_down",
                    service,
                    "critical",
                    {"exit_codes": exit_codes, "oom_killed": any(c["oom_killed"] for c in containers)},
                )
            elif not down:
                bus.clear(f"down:{service}")

            unready = not down and any(c["health"] == "unhealthy" for c in containers)
            self.unready_polls[service] = self.unready_polls.get(service, 0) + 1 if unready else 0
            if self.unready_polls[service] >= CONFIRM_POLLS:
                bus.fire(f"unready:{service}", "not_ready", service, "critical", {"containers": [c["name"] for c in containers]})
            elif not unready:
                bus.clear(f"unready:{service}")
        self.last = services

    async def run(self) -> None:
        while True:
            try:
                self._observe(await platform.services())
            except Exception as exc:  # noqa: BLE001
                log.warning("Platform poll failed: %s", exc)
            await asyncio.sleep(POLL_S)


container_detector = ContainerDetector()
