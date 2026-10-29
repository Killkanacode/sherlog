"""Outbound HTTP to other services, with timeouts, retries and client metrics.

Retry behaviour comes from the environment:
  RETRIES           extra attempts after the first one (default 2)
  RETRY_BACKOFF_MS  base for exponential backoff with jitter; 0 disables waiting
"""

import asyncio
import logging
import random
import time

import httpx
from opentelemetry import metrics

from common.config import env_int

log = logging.getLogger("downstream")

_meter = metrics.get_meter("common.http.client")
_client_duration = _meter.create_histogram(
    "http.client.duration",
    unit="s",
    description="Duration of outbound HTTP requests, one sample per attempt",
)

RETRYABLE_STATUS = {502, 503, 504}


class DownstreamError(Exception):
    """A dependency failed after all attempts.

    kind is one of: timeout, connect_error, http_error.
    """

    def __init__(self, peer: str, kind: str, status_code: int | None = None, detail: str = ""):
        self.peer = peer
        self.kind = kind
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{peer} {kind}" + (f" {status_code}" if status_code else "") + (f": {detail}" if detail else ""))


class Downstream:
    def __init__(self, peer: str, base_url: str, timeout_s: float, retries: int | None = None):
        self.peer = peer
        self.retries = env_int("RETRIES", 2) if retries is None else retries
        self.backoff_ms = env_int("RETRY_BACKOFF_MS", 100)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Return the response for any non-retryable status (including 4xx).

        Raises DownstreamError when attempts are exhausted on timeouts,
        connection errors or retryable 5xx responses.
        """
        attempts = self.retries + 1
        last_error: DownstreamError | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            outcome, status = "success", 0
            try:
                response = await self._client.request(method, path, **kwargs)
                status = response.status_code
                if status in RETRYABLE_STATUS:
                    outcome = "http_error"
                    last_error = DownstreamError(self.peer, "http_error", status, response.text[:200])
                elif status >= 500:
                    outcome = "http_error"
                    raise DownstreamError(self.peer, "http_error", status, response.text[:200])
                else:
                    return response
            except httpx.TimeoutException:
                outcome = "timeout"
                last_error = DownstreamError(self.peer, "timeout")
            except httpx.TransportError as exc:
                outcome = "connect_error"
                last_error = DownstreamError(self.peer, "connect_error", detail=type(exc).__name__)
            finally:
                _client_duration.record(
                    time.perf_counter() - started,
                    {"peer": self.peer, "method": method, "outcome": outcome, "status_code": str(status)},
                )

            if attempt < attempts:
                log.warning(
                    "Retrying %s %s %s after %s (attempt %d/%d)",
                    method,
                    self.peer,
                    path,
                    last_error.kind,
                    attempt,
                    attempts,
                    extra={"peer": self.peer, "attempt": attempt, "error_kind": last_error.kind},
                )
                if self.backoff_ms > 0:
                    delay = self.backoff_ms * (2 ** (attempt - 1)) * random.uniform(0.5, 1.5)
                    await asyncio.sleep(delay / 1000)
        raise last_error
