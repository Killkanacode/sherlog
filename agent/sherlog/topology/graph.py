"""Service dependency graph, built from traces.

Tempo's metrics generator pairs client and server spans into
traces_service_graph_request_total{client, server}; uninstrumented
dependencies (postgresql, redis, card-provider) appear as virtual servers.
Edges are remembered for an hour so a service that goes dark stays on the map.
"""

import asyncio
import logging
from datetime import timedelta

from sherlog.config import SERVICES
from sherlog.ingest.clients import prometheus
from sherlog.signals import utcnow

log = logging.getLogger("sherlog.topology")

REFRESH_S = 30
EDGE_TTL = timedelta(hours=1)


class Topology:
    def __init__(self) -> None:
        self.edges: dict[tuple[str, str], dict] = {}

    async def refresh(self) -> None:
        rates = await prometheus.query("sum by (client, server) (rate(traces_service_graph_request_total[5m]))")
        failed = await prometheus.query("sum by (client, server) (rate(traces_service_graph_request_failed_total[5m]))")
        failures = {(r["metric"]["client"], r["metric"]["server"]): float(r["value"][1]) for r in failed}
        now = utcnow()
        for r in rates:
            client, server = r["metric"].get("client"), r["metric"].get("server")
            if not client or not server or client == "user":
                continue
            rps = float(r["value"][1])
            self.edges[(client, server)] = {
                "rps": round(rps, 3),
                "error_rate": round(failures.get((client, server), 0.0) / rps, 4) if rps else 0.0,
                "last_seen": now,
            }
        for key in [k for k, v in self.edges.items() if now - v["last_seen"] > EDGE_TTL]:
            del self.edges[key]

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("Topology refresh failed: %s", exc)
            await asyncio.sleep(REFRESH_S)

    def downstream(self, service: str) -> set[str]:
        return {server for client, server in self.edges if client == service}

    def upstream(self, service: str) -> set[str]:
        return {client for client, server in self.edges if server == service}

    def neighbours(self, service: str) -> set[str]:
        return self.downstream(service) | self.upstream(service)

    def connected(self, a: str, b: str, max_hops: int = 2) -> bool:
        if a == b:
            return True
        frontier, seen = {a}, {a}
        for _ in range(max_hops):
            frontier = {n for node in frontier for n in self.neighbours(node)} - seen
            if b in frontier:
                return True
            seen |= frontier
        return False

    def nodes(self) -> list[str]:
        names = set(SERVICES)
        for client, server in self.edges:
            names.update((client, server))
        return sorted(names)

    def to_dict(self) -> dict:
        return {
            "nodes": [{"id": n, "kind": "service" if n in SERVICES else "dependency"} for n in self.nodes()],
            "edges": [
                {"source": c, "target": s, "rps": e["rps"], "error_rate": e["error_rate"]}
                for (c, s), e in sorted(self.edges.items())
            ],
        }


topology = Topology()
