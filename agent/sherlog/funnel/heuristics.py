"""Root-cause hint without an LLM.

Among services with *impact* signals (errors, latency, saturation, crashes,
silence, exceptions), prefer the deepest one in the dependency graph — its
callers are failing but the things it calls are healthy — then the one with
the most affected callers, then the earliest anomaly. Traffic shifts and
log-only signals are supporting evidence: they usually describe a victim or
noise, not a cause. The result is passed to the investigator as a hint.
"""

from collections import defaultdict

from sherlog.signals import Signal
from sherlog.topology.graph import topology

IMPACT_TYPES = {"container_down", "container_restart", "not_ready", "log_silence", "new_exception", "exception_spike"}


def is_impact(signal: Signal) -> bool:
    if signal.type == "metric_anomaly":
        return bool(signal.details.get("impact", True))
    return signal.type in IMPACT_TYPES


def rank_root_cause(signals: list[Signal]) -> dict:
    impact: dict[str, list[Signal]] = defaultdict(list)
    support: dict[str, list[Signal]] = defaultdict(list)
    for signal in signals:
        (impact if is_impact(signal) else support)[signal.service].append(signal)

    anomalous = set(impact)
    ranking = []
    for service, service_signals in impact.items():
        bad_deps = topology.downstream(service) & anomalous
        bad_callers = topology.upstream(service) & anomalous
        first = min(s.started_at for s in service_signals)
        ranking.append(
            {
                "service": service,
                "deepest": not bad_deps,
                "unhealthy_dependencies": sorted(bad_deps),
                "affected_callers": sorted(bad_callers),
                "first_anomaly": first.isoformat(),
                "impact_signals": sorted({_label(s) for s in service_signals}),
                "supporting_signals": sorted({_label(s) for s in support.get(service, [])}),
            }
        )
    ranking.sort(key=lambda r: (not r["deepest"], -len(r["affected_callers"]), r["first_anomaly"]))
    if not ranking:
        return {"service": None, "reason": "no impact signals yet", "ranking": []}

    top = ranking[0]
    reason = (
        f"{top['service']} is the deepest anomalous service"
        + (f": its callers {', '.join(top['affected_callers'])} are failing" if top["affected_callers"] else "")
        + (" while its own dependencies look healthy" if top["deepest"] else "")
        + f"; first anomaly at {top['first_anomaly']}."
    )
    return {"service": top["service"], "reason": reason, "ranking": ranking}


def _label(signal: Signal) -> str:
    metric = signal.details.get("metric")
    if metric:
        peer = signal.details.get("peer")
        return f"{metric}{'->' + peer if peer else ''} {signal.details.get('direction', '')}".strip()
    return signal.type
