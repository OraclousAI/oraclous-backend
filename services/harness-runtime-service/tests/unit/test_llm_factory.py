"""LLM factory (slice 4): fake vs live; openai-compatible wired, other shapes fail-closed."""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.llm.factory import (
    LLMConfigError,
    build_fake_client,
    build_live_client,
)
from oraclous_harness_runtime_service.domain.llm.fake import FakeLLMClient
from oraclous_harness_runtime_service.domain.llm.openai_compatible import OpenAICompatibleClient

pytestmark = pytest.mark.unit


def test_fake_client() -> None:
    assert isinstance(build_fake_client(), FakeLLMClient)


def test_openai_compatible_is_wired() -> None:
    client = build_live_client(
        protocol_shape="openai-compatible",
        base_url="https://router.test/api/v1",
        api_key="k",
        model="vendor/model",
        timeout=10.0,
    )
    assert isinstance(client, OpenAICompatibleClient)
    assert client.protocol_shape == "openai-compatible"


@pytest.mark.parametrize("shape", ["native", "gemini-compatible", "bogus"])
def test_unwired_shapes_fail_closed(shape: str) -> None:
    with pytest.raises(LLMConfigError):
        build_live_client(protocol_shape=shape, base_url="x", api_key="k", model="m", timeout=10.0)
