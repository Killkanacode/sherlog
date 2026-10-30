"""Sherlog settings, read once from the environment."""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


def _price(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else None


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return value.strip().lower() in ("1", "true", "yes") if value else default


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
    resolve_quiet_s: int = _int("SHERLOG_RESOLVE_QUIET_S", 90)

    # Models. Endpoints, auth and IDs are TODO(VERIFY) against Meta's docs; with
    # SHERLOG_FAKE_LLM=true no model API is called (local development only).
    fake_llm: bool = _bool("SHERLOG_FAKE_LLM", False)
    llm_timeout_s: float = _float("SHERLOG_LLM_TIMEOUT_S", 90.0)
    meta_api_base_url: str = os.environ.get("META_API_BASE_URL", "")
    meta_api_key: str = os.environ.get("META_API_KEY", "")
    model_investigator: str = os.environ.get("MODEL_INVESTIGATOR", "")
    open_model_base_url: str = os.environ.get("OPEN_MODEL_BASE_URL", "")
    open_model_api_key: str = os.environ.get("OPEN_MODEL_API_KEY", "")
    open_model_id: str = os.environ.get("OPEN_MODEL_ID", "")
    # USD per million tokens; unset means "unknown" and cost is reported as null.
    price_investigator_in: float | None = _price("MODEL_INVESTIGATOR_PRICE_IN")
    price_investigator_out: float | None = _price("MODEL_INVESTIGATOR_PRICE_OUT")
    price_open_in: float | None = _price("OPEN_MODEL_PRICE_IN")
    price_open_out: float | None = _price("OPEN_MODEL_PRICE_OUT")

    # Investigation
    investigate: bool = _bool("SHERLOG_INVESTIGATE", True)
    max_steps: int = _int("SHERLOG_MAX_STEPS", 12)
    token_budget: int = _int("SHERLOG_TOKEN_BUDGET_PER_INCIDENT", 60000)
    tool_timeout_s: float = _float("SHERLOG_TOOL_TIMEOUT_S", 20.0)


settings = Settings()

# Services Sherlog watches (the shop). Datastores appear via the service graph.
SERVICES = ("gateway", "orders", "payments", "inventory")
