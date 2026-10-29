"""Root-cause hint without an LLM.

Among services with *impact* signals (errors, latency, saturation, crashes,
silence, exceptions), prefer the deepest one in the dependency graph — its
callers are failing but the things it calls are healthy — then the one with
the most affected callers, then the earliest anomaly. Traffic shifts and
log-only signals are supporting evidence: they usually describe a victim or
noise, not a cause. Traffic also changes how two signals read: a service
whose inbound traffic surged before it degraded is being overloaded by its
callers (a retry storm), so the blame moves up to them; and a service that
goes quiet after its traffic dropped is starved, not broken, so its silence
is supporting evidence. The result is passed to the investigator as a hint.
"""

from collections import defaultdict
from datetime import datetime

from sherlog.signals import Signal
from sherlog.topology.graph import topology

IMPACT_TYPES = {"container_down", "container_restart", "not_ready", "log_silence", "new_exception", "exception_spike"}


def is_impact(signal: Signal) -> bool:
    if signal.type == "metric_anomaly":
        return bool(signal.details.get("impact", True))
    return signal.type in IMPACT_TYPES


def rank_root_cause(signals: list[Signal]) -> dict:
    traffic_drop: dict[str, datetime] = {}
    for signal in signals:
        if _is_traffic(signal, "down"):
            traffic_drop[signal.service] = min(signal.started_at, traffic_drop.get(signal.service, signal.started_at))

    impact: dict[str, list[Signal]] = defaultdict(list)
    support: dict[str, list[Signal]] = defaultdict(list)
    for signal in signals:
        starved = signal.type == "log_silence" and traffic_drop.get(signal.service, signal.started_at) < signal.started_at
        (impact if is_impact(signal) and not starved else support)[signal.service].append(signal)

    anomalous = set(impact)
    first_impact = {service: min(s.started_at for s in found) for service, found in impact.items()}
    overloaded = {
        service
        for service in anomalous
        if topology.upstream(service) & anomalous
        and any(_is_traffic(s, "up") and s.started_at <= first_impact[service] for s in support.get(service, []))
    }
    ranking = []
    for service, service_signals in impact.items():
        bad_deps = topology.downstream(service) & (anomalous - overloaded)
        bad_callers = topology.upstream(service) & anomalous
        ranking.append(
            {
                "service": service,
                "deepest": not bad_deps,
                "overloaded_by_callers": service in overloaded,
                "unhealthy_dependencies": sorted(bad_deps),
                "affected_callers": sorted(bad_callers),
                "first_anomaly": first_impact[service].isoformat(),
                "impact_signals": sorted({_label(s) for s in service_signals}),
                "supporting_signals": sorted({_label(s) for s in support.get(service, [])}),
            }
        )
    ranking.sort(
        key=lambda r: (r["overloaded_by_callers"], not r["deepest"], -len(r["affected_callers"]), r["first_anomaly"])
    )
    if not ranking:
        return {"service": None, "reason": "no impact signals yet", "ranking": []}

    top = ranking[0]
    reason = (
        f"{top['service']} is the deepest anomalous service"
        + (f": its callers {', '.join(top['affected_callers'])} are failing" if top["affected_callers"] else "")
        + (" while its own dependencies look healthy" if top["deepest"] else "")
        + f"; first anomaly at {top['first_anomaly']}."
    )
    victims = sorted(overloaded & topology.downstream(top["service"]))
    if victims:
        reason += f" {', '.join(victims)} saw a traffic surge before degrading, so it is likely overloaded by {top['service']}."
    return {"service": top["service"], "reason": reason, "ranking": ranking}


def _is_traffic(signal: Signal, direction: str) -> bool:
    return signal.details.get("metric") == "traffic" and signal.details.get("direction") == direction


def _label(signal: Signal) -> str:
    metric = signal.details.get("metric")
    if metric:
        peer = signal.details.get("peer")
        return f"{metric}{'->' + peer if peer else ''} {signal.details.get('direction', '')}".strip()
    return signal.type
