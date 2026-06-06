"""LLM client factory (ORAA-4 §21 domain layer; ADR-007).

`fake` mode → the key-free deterministic client (CI/smoke). `live` mode → a real client built from
the OHM model's ``protocol_shape`` + a BYOM key (resolved via the credential-broker; ADR-008 — no
platform fallback). Slice 4 wires the **openai-compatible** shape (which OpenRouter serves for
Claude/OpenAI/Gemini/etc. behind one key); the ``native`` (Anthropic) and ``gemini`` shapes raise a
clear config error until their direct providers are wired. Fail-closed: an unwired shape never
silently degrades to a billable default.
"""

from __future__ import annotations

from oraclous_harness_runtime_service.domain.llm.base import LLMClient
from oraclous_harness_runtime_service.domain.llm.fake import FakeLLMClient
from oraclous_harness_runtime_service.domain.llm.openai_compatible import OpenAICompatibleClient


class LLMConfigError(Exception):
    """The configured LLM cannot be built (unwired shape, or a missing BYOM credential)."""


def build_fake_client() -> LLMClient:
    return FakeLLMClient()


def build_live_client(
    *, protocol_shape: str, base_url: str, api_key: str, model: str, timeout: float
) -> LLMClient:
    if protocol_shape == "openai-compatible":
        return OpenAICompatibleClient(
            base_url=base_url, api_key=api_key, model=model, timeout=timeout
        )
    raise LLMConfigError(
        f"protocol_shape {protocol_shape!r} is not wired in this build; use 'openai-compatible' "
        "(e.g. a Claude/OpenAI/Gemini model via OpenRouter)"
    )
