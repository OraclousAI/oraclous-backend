"""LLM client factory (ORAA-4 §21 domain layer; ADR-007).

Selects the ``LLMClient`` by ``HARNESS_LLM_MODE``. Slice 1 wires the key-free ``fake`` client; the
real protocol-shape clients (native / openai-compatible / gemini-compatible) and BYOM credential
resolution via the credential-broker are added in slice 4. Fail-closed: an unwired mode is a config
error, never a silent default to a billable provider.
"""

from __future__ import annotations

from oraclous_harness_runtime_service.domain.llm.base import LLMClient
from oraclous_harness_runtime_service.domain.llm.fake import FakeLLMClient


class LLMConfigError(Exception):
    """The configured LLM mode cannot be built (unwired mode, or missing BYOM credential)."""


def build_llm_client(mode: str) -> LLMClient:
    if mode == "fake":
        return FakeLLMClient()
    raise LLMConfigError(
        f"HARNESS_LLM_MODE={mode!r} is not available in this build (only 'fake' is wired; "
        "real provider shapes land in slice 4)"
    )
