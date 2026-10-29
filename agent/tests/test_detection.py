from datetime import datetime, timedelta, timezone

import pytest

from sherlog.detectors.baseline import Ewma
from sherlog.detectors.logs import fingerprint
from sherlog.funnel.heuristics import rank_root_cause
from sherlog.signals import Signal
from sherlog.topology.graph import topology

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

STACK = """Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/starlette/routing.py", line 661, in app
    await route.handle(scope, receive, send)
  File "/app/orders/main.py", line {line}, in create_order
    total = pricing.order_total(lines)
  File "/app/orders/pricing.py", line 12, in order_total
    return sum(line["price"] * line["qty"] for line in lines)
KeyError: 'price'
"""


def test_fingerprint_ignores_library_frames_and_line_numbers():
    a, frames = fingerprint("KeyError", STACK.format(line=70))
    b, _ = fingerprint("KeyError", STACK.format(line=99))
    assert a == b
    assert frames == ["orders/main.py:create_order", "orders/pricing.py:order_total"]


def test_fingerprint_depends_on_exception_type():
    assert fingerprint("KeyError", STACK.format(line=1))[0] != fingerprint("TypeError", STACK.format(line=1))[0]


def test_ewma_tracks_mean_and_spread():
    ewma = Ewma(alpha=0.1)
    for value in [10, 12, 8, 11, 9] * 20:
        ewma.update(value)
    assert ewma.mean == pytest.approx(10, abs=0.5)
    assert 0.5 < ewma.std < 2.5


@pytest.fixture
def shop_graph():
    saved = dict(topology.edges)
    topology.edges.clear()
    for client, server in [
        ("gateway", "orders"), ("gateway", "inventory"), ("orders", "inventory"), ("orders", "payments"),
        ("inventory", "postgresql"), ("inventory", "redis"), ("payments", "redis"), ("orders", "postgresql"),
    ]:
        topology.edges[(client, server)] = {"rps": 1.0, "error_rate": 0.0, "last_seen": T0}
    yield
    topology.edges.clear()
    topology.edges.update(saved)


def metric(service: str, name: str, offset_s: int, impact: bool = True, direction: str = "up") -> Signal:
    return Signal(
        "metric_anomaly", service, "critical", T0 + timedelta(seconds=offset_s),
        {"metric": name, "direction": direction, "impact": impact}, f"{service}:{name}",
    )


def test_hint_prefers_deepest_anomalous_service(shop_graph):
    # Slow inventory: callers degrade after it.
    signals = [
        metric("gateway", "error_rate", 40),
        metric("orders", "latency_p95", 20),
        metric("inventory", "latency_p95", 0),
    ]
    assert rank_root_cause(signals)["service"] == "inventory"


def test_hint_ignores_log_only_red_herring(shop_graph):
    # Noisy-but-harmless inventory deploy first, real payments bug later.
    noise = Signal("new_log_template", "inventory", "critical", T0, {"template": "DeprecationWarning <*>"}, "noise")
    signals = [
        noise,
        metric("payments", "error_rate", 30),
        metric("orders", "error_rate", 35),
        metric("gateway", "error_rate", 40),
    ]
    assert rank_root_cause(signals)["service"] == "payments"


def test_hint_treats_traffic_drop_as_a_victim(shop_graph):
    # orders can't reach inventory: payments sees less traffic but isn't the cause.
    signals = [
        metric("orders", "client_failure_rate", 0),
        metric("gateway", "error_rate", 10),
        metric("payments", "traffic", 5, impact=False, direction="down"),
    ]
    assert rank_root_cause(signals)["service"] == "orders"


def test_hint_treats_silence_after_a_traffic_drop_as_starvation(shop_graph):
    # orders breaks and stops calling payments, which then goes quiet.
    silence = Signal("log_silence", "payments", "critical", T0 + timedelta(seconds=50), {}, "silence:payments")
    signals = [
        metric("orders", "error_rate", 0),
        metric("payments", "traffic", 10, impact=False, direction="down"),
        metric("gateway", "error_rate", 20),
        silence,
    ]
    assert rank_root_cause(signals)["service"] == "orders"


def test_hint_blames_the_caller_in_a_retry_storm(shop_graph):
    # orders hammers payments with retries: payments' traffic surges, then it slows down.
    signals = [
        metric("orders", "client_failure_rate", 0),
        metric("payments", "traffic", 10, impact=False),
        metric("orders", "error_rate", 20),
        metric("payments", "latency_p95", 30),
        metric("gateway", "error_rate", 40),
    ]
    hint = rank_root_cause(signals)
    assert hint["service"] == "orders"
    assert "payments saw a traffic surge" in hint["reason"]


def test_hint_keeps_a_slow_service_whose_traffic_rose_later(shop_graph):
    # inventory slows first; callers' retries raise its traffic afterwards.
    signals = [
        metric("inventory", "latency_p95", 0),
        metric("orders", "latency_p95", 10),
        metric("inventory", "traffic", 20, impact=False),
    ]
    assert rank_root_cause(signals)["service"] == "inventory"


def test_hint_points_at_datastore(shop_graph):
    signals = [
        metric("redis", "dependency_failure_rate", 0),
        metric("payments", "error_rate", 5),
        metric("orders", "error_rate", 10),
    ]
    assert rank_root_cause(signals)["service"] == "redis"
