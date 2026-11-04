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

    # Models (Meta Model API, dev.meta.ai/docs). With SHERLOG_FAKE_LLM=true no
    # model API is called (local development only).
    fake_llm: bool = _bool("SHERLOG_FAKE_LLM", False)
    llm_timeout_s: float = _float("SHERLOG_LLM_TIMEOUT_S", 90.0)
    meta_api_base_url: str = os.environ.get("META_API_BASE_URL") or "https://api.meta.ai/v1"
    meta_api_key: str = os.environ.get("META_API_KEY", "")
    model_investigator: str = os.environ.get("MODEL_INVESTIGATOR") or "muse-spark-1.3"
    # minimal | low | medium | high | xhigh | max; Muse Spark can't skip reasoning.
    investigator_reasoning: str = os.environ.get("MODEL_INVESTIGATOR_REASONING") or "low"
    # Muse Spark Open on a partner (Baseten, Modal, ...). Until it is set,
    # triage runs on Muse Spark at minimal reasoning.
    open_model_base_url: str = os.environ.get("OPEN_MODEL_BASE_URL", "")
    open_model_api_key: str = os.environ.get("OPEN_MODEL_API_KEY", "")
    open_model_id: str = os.environ.get("OPEN_MODEL_ID", "")
    open_model_reasoning: str = os.environ.get("OPEN_MODEL_REASONING", "")
    # USD per million tokens; unset means "unknown" and cost is reported as null.
    price_investigator_in: float | None = _price("MODEL_INVESTIGATOR_PRICE_IN")
    price_investigator_out: float | None = _price("MODEL_INVESTIGATOR_PRICE_OUT")
    price_investigator_cached: float | None = _price("MODEL_INVESTIGATOR_PRICE_CACHED")  # cached input
    price_open_in: float | None = _price("OPEN_MODEL_PRICE_IN")
    price_open_out: float | None = _price("OPEN_MODEL_PRICE_OUT")

    # Model guardrails: a month's spend and tokens (0 = no limit), how many
    # investigations run at once, and a breaker that pauses model calls after
    # consecutive failures instead of hammering a broken API.
    monthly_spend_limit_usd: float = _float("SHERLOG_MONTHLY_SPEND_LIMIT_USD", 0.0)
    monthly_token_limit: int = _int("SHERLOG_MONTHLY_TOKEN_LIMIT", 0)
    max_concurrent_investigations: int = _int("SHERLOG_MAX_CONCURRENT_INVESTIGATIONS", 2)
    breaker_failures: int = _int("SHERLOG_LLM_BREAKER_FAILURES", 3)
    breaker_cooldown_s: int = _int("SHERLOG_LLM_BREAKER_COOLDOWN_S", 300)

    # Investigation
    investigate: bool = _bool("SHERLOG_INVESTIGATE", True)
    max_steps: int = _int("SHERLOG_MAX_STEPS", 12)
    token_budget: int = _int("SHERLOG_TOKEN_BUDGET_PER_INCIDENT", 100000)
    tool_timeout_s: float = _float("SHERLOG_TOOL_TIMEOUT_S", 20.0)

    # Fixer: Muse Code writes a fix PR for code bugs (empty = off); a job's longest wait.
    fixer_url: str = os.environ.get("FIXER_URL", "")
    fixer_wait_s: int = _int("SHERLOG_FIXER_WAIT_S", 1200)

    # Actions. Autonomy and dry run are only the defaults: PUT /settings changes
    # them at runtime and the choice is kept in Sherlog's database.
    autonomy: str = os.environ.get("SHERLOG_AUTONOMY", "L1")  # L0 suggest | L1 approve | L2 auto for low risk
    dry_run: bool = _bool("SHERLOG_DRY_RUN", False)
    auto_min_confidence: float = _float("SHERLOG_AUTO_MIN_CONFIDENCE", 0.7)
    max_action_cycles: int = _int("SHERLOG_MAX_ACTION_CYCLES", 2)
    verify_timeout_s: int = _int("SHERLOG_VERIFY_TIMEOUT_S", 180)  # give up on recovery after this
    verify_healthy_s: int = _int("SHERLOG_VERIFY_HEALTHY_S", 30)  # healthy this long = recovered
    verify_poll_s: float = _float("SHERLOG_VERIFY_POLL_S", 10.0)
    # Guardrails on what Sherlog proposes (a human's request skips the first two).
    max_auto_actions_per_hour: int = _int("SHERLOG_MAX_ACTIONS_PER_HOUR", 6)
    service_cooldown_s: int = _int("SHERLOG_SERVICE_COOLDOWN_S", 600)

    # Access. API keys are "name:role:sha256(token)" entries; SHERLOG_ADMIN_TOKEN
    # sets the first admin's token (otherwise one is generated and logged once).
    api_keys: str = os.environ.get("SHERLOG_API_KEYS", "")
    admin_token: str = os.environ.get("SHERLOG_ADMIN_TOKEN", "")
    events_token: str = os.environ.get("SHERLOG_EVENTS_TOKEN", "")  # platform-api's push of change events
    secret_key: str = os.environ.get("SHERLOG_SECRET_KEY", "")  # signs sessions; generated and stored if unset
    session_hours: int = _int("SHERLOG_SESSION_HOURS", 12)
    secure_cookies: bool = _bool("SHERLOG_SECURE_COOKIES", False)  # true behind HTTPS
    cors_origins: str = os.environ.get("SHERLOG_CORS_ORIGINS", "")  # comma-separated; empty = same origin only


settings = Settings()

# Services Sherlog watches (the shop). Datastores appear via the service graph.
SERVICES = ("gateway", "orders", "payments", "inventory")
