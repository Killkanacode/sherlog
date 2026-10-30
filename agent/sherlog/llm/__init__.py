"""Model clients, one per role, configured from the environment.

  investigator  Muse Spark on the Meta Model API (tool calling + JSON output)
  triage        Muse Spark Open on Baseten or Modal (high volume, low cost)

Muse Code (fixer) and Muse Voice Transcribe join in Phase 6.
"""

from functools import cache

from sherlog.config import settings
from sherlog.llm.base import ChatModel, LLMError
from sherlog.llm.chat_api import ChatCompletionsModel
from sherlog.llm.fake import FakeInvestigatorModel, FakeTriageModel

__all__ = ["ChatModel", "LLMError", "investigator_model", "triage_model", "mode"]


def mode() -> str:
    return "fake" if settings.fake_llm else "live"


@cache
def investigator_model() -> ChatModel:
    if settings.fake_llm:
        return FakeInvestigatorModel()
    return ChatCompletionsModel(
        settings.meta_api_base_url, settings.meta_api_key, settings.model_investigator,
        timeout_s=settings.llm_timeout_s,
        price_in=settings.price_investigator_in, price_out=settings.price_investigator_out,
    )


@cache
def triage_model() -> ChatModel:
    if settings.fake_llm:
        return FakeTriageModel()
    return ChatCompletionsModel(
        settings.open_model_base_url, settings.open_model_api_key, settings.open_model_id,
        timeout_s=settings.llm_timeout_s,
        price_in=settings.price_open_in, price_out=settings.price_open_out,
    )
