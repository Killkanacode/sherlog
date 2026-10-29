"""HTTP clients for everything Sherlog observes: Prometheus, Loki, Tempo and
the platform API. These are the only windows Sherlog has onto the system."""

from datetime import datetime

import httpx

from sherlog.config import settings


def _ns(ts: datetime) -> str:
    return str(int(ts.timestamp() * 1e9))


def normalize_trace_id(trace_id: str) -> str:
    """Tempo drops leading zeros from trace IDs; logs keep all 32 hex digits."""
    return trace_id.lower().rjust(32, "0")


class Prometheus:
    def __init__(self, base_url: str = settings.prometheus_url) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=15.0)

    async def query(self, promql: str, at: datetime | None = None) -> list[dict]:
        params = {"query": promql}
        if at:
            params["time"] = str(at.timestamp())
        response = await self._http.get("/api/v1/query", params=params)
        response.raise_for_status()
        return response.json()["data"]["result"]

    async def query_range(self, promql: str, start: datetime, end: datetime, step_s: float) -> list[dict]:
        response = await self._http.get(
            "/api/v1/query_range",
            params={"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step_s},
        )
        response.raise_for_status()
        return response.json()["data"]["result"]


class Loki:
    def __init__(self, base_url: str = settings.loki_url) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def query_range(
        self, logql: str, start: datetime, end: datetime, limit: int = 1000, direction: str = "forward"
    ) -> list[dict]:
        """Log streams: [{"stream": {labels + structured metadata}, "values": [[ns, line], ...]}]."""
        response = await self._http.get(
            "/loki/api/v1/query_range",
            params={"query": logql, "start": _ns(start), "end": _ns(end), "limit": limit, "direction": direction},
        )
        response.raise_for_status()
        return response.json()["data"]["result"]

    async def query(self, logql: str, at: datetime | None = None) -> list[dict]:
        """Instant metric query, e.g. sum by (service_name) (count_over_time(...[1m]))."""
        params = {"query": logql}
        if at:
            params["time"] = _ns(at)
        response = await self._http.get("/loki/api/v1/query", params=params)
        response.raise_for_status()
        return response.json()["data"]["result"]


class Tempo:
    def __init__(self, base_url: str = settings.tempo_url) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=20.0)

    async def search(self, traceql: str, start: datetime, end: datetime, limit: int = 20) -> list[dict]:
        response = await self._http.get(
            "/api/search",
            params={"q": traceql, "start": int(start.timestamp()), "end": int(end.timestamp()), "limit": limit},
        )
        response.raise_for_status()
        traces = response.json().get("traces", [])
        for trace in traces:
            trace["traceID"] = normalize_trace_id(trace["traceID"])
        return traces

    async def trace(self, trace_id: str) -> dict:
        response = await self._http.get(f"/api/v2/traces/{normalize_trace_id(trace_id)}")
        response.raise_for_status()
        return response.json()


class Platform:
    """The deploy platform's read API (plus actions, used from Phase 4)."""

    def __init__(self, base_url: str = settings.platform_url) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def services(self) -> list[dict]:
        response = await self._http.get("/services")
        response.raise_for_status()
        return response.json()

    async def events(self, since: str | None = None, limit: int = 200) -> list[dict]:
        params = {"limit": limit}
        if since:
            params["since"] = since
        response = await self._http.get("/events", params=params)
        response.raise_for_status()
        return response.json()


prometheus = Prometheus()
loki = Loki()
tempo = Tempo()
platform = Platform()
