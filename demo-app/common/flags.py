"""Feature flags polled from the platform API (like a LaunchDarkly-style SDK).

If FLAGS_URL is unset the client is disabled and every flag reads as off.
"""

import asyncio
import logging

import httpx

from common.config import env_float, env_str

log = logging.getLogger("flags")


class FlagClient:
    def __init__(self) -> None:
        self._url = env_str("FLAGS_URL")
        self._interval = env_float("FLAGS_POLL_S", 5.0)
        self._flags: dict[str, bool] = {}
        self._task: asyncio.Task | None = None
        self._healthy = True

    def is_enabled(self, name: str) -> bool:
        return bool(self._flags.get(name, False))

    async def start(self) -> None:
        if self._url:
            self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _poll(self) -> None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            while True:
                try:
                    response = await client.get(self._url)
                    response.raise_for_status()
                    flags = {str(k): bool(v) for k, v in response.json().items()}
                    if flags != self._flags:
                        log.info("Feature flags updated: %s", flags, extra={"flags": flags})
                    self._flags = flags
                    self._healthy = True
                except (httpx.HTTPError, ValueError) as exc:
                    if self._healthy:
                        log.warning("Feature flag refresh failed, keeping last known values: %s", exc)
                    self._healthy = False
                await asyncio.sleep(self._interval)
