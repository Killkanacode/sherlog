"""Model clients, one per role, configured from the environment.

  investigator  Muse Spark on the Meta Model API (tool calling + JSON output)
  triage        Muse Spark Open on a partner such as Baseten or Modal (high
                volume, low cost); Muse Spark at minimal reasoning until one
                is configured, so a missing endpoint never stops an
                investigation

Muse Code (fixer) and Muse Voice Transcribe join in Phase 6.

Every client is wrapped in GuardedModel: secrets are redacted from what is
sent, the monthly budget is enforced and a breaker pauses calls after
repeated failures.
"""

import logging
from functools import cache

from sherlog.config import settings
from sherlog.llm.base import ChatModel, LLMError
from sherlog.llm.chat_api import ChatCompletionsModel
from sherlog.llm.fake import FakeInvestigatorModel, FakeTriageModel
from sherlog.llm.guard import GuardedModel

__all__ = ["ChatModel", "LLMError", "investigator_model", "triage_model", "mode", "describe"]

log = logging.getLogger("sherlog.llm")


def mode() -> str:
    return "fake" if settings.fake_llm else "live"


def open_model_configured() -> bool:
    return bool(settings.open_model_base_url and settings.open_model_api_key and settings.open_model_id)


def describe() -> dict:
    """Which model plays each role, for the console."""
    if settings.fake_llm:
        return {"investigator": "fake", "triage": "fake"}
    triage = settings.open_model_id if open_model_configured() else f"{settings.model_investigator} (Muse Spark Open not set)"
    return {"investigator": settings.model_investigator, "reasoning": settings.investigator_reasoning, "triage": triage}


@cache
def investigator_model() -> ChatModel:
    if settings.fake_llm:
        return GuardedModel(FakeInvestigatorModel())
    return GuardedModel(
        ChatCompletionsModel(
            settings.meta_api_base_url, settings.meta_api_key, settings.model_investigator,
            timeout_s=settings.llm_timeout_s,
            price_in=settings.price_investigator_in, price_out=settings.price_investigator_out,
            reasoning_effort=settings.investigator_reasoning,
        )
    )


@cache
def triage_model() -> ChatModel:
    if settings.fake_llm:
        return GuardedModel(FakeTriageModel())
    if not open_model_configured():
        log.warning("Muse Spark Open is not configured; triage runs on %s", settings.model_investigator)
        return GuardedModel(
            ChatCompletionsModel(
                settings.meta_api_base_url, settings.meta_api_key, settings.model_investigator,
                timeout_s=settings.llm_timeout_s,
                price_in=settings.price_investigator_in, price_out=settings.price_investigator_out,
                reasoning_effort="minimal",
            )
        )
    return GuardedModel(
        ChatCompletionsModel(
            settings.open_model_base_url, settings.open_model_api_key, settings.open_model_id,
            timeout_s=settings.llm_timeout_s,
            price_in=settings.price_open_in, price_out=settings.price_open_out,
            reasoning_effort=settings.open_model_reasoning,
        )
    )
