"""Sherlog settings, read once from the environment."""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
    loki_url: str = os.environ.get("LOKI_URL", "http://loki:3100")
    tempo_url: str = os.environ.get("TEMPO_URL", "http://tempo:3200")
    platform_url: str = os.environ.get("PLATFORM_API_URL", "http://platform-api:8050")
    database_url: str = os.environ.get("SHERLOG_DATABASE_URL", "postgresql+asyncpg://sherlog:sherlog@sherlog-db:5432/sherlog")

    # Detection
    detect_interval_s: float = _float("SHERLOG_DETECT_INTERVAL_S", 10.0)
    learning_s: int = _int("SHERLOG_LEARNING_S", 600)  # history used to seed baselines
    persistence: int = _int("SHERLOG_PERSISTENCE", 3)  # consecutive anomalous ticks
    log_poll_s: float = _float("SHERLOG_LOG_POLL_S", 5.0)

    # Incidents
    incident_window_s: int = _int("SHERLOG_INCIDENT_WINDOW_S", 120)
    resolve_quiet_s: int = _int("SHERLOG_RESOLVE_QUIET_S", 180)


settings = Settings()

# Services Sherlog watches (the shop). Datastores appear via the service graph.
SERVICES = ("gateway", "orders", "payments", "inventory")
