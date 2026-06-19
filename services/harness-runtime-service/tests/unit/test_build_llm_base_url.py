"""HarnessExecutionService._build_llm — base-URL selection + the egress guard wiring (fakes).

Covers: a connection-supplied ``base_url`` wins over the server map and IS guarded; a connection
without ``base_url`` falls back to the server map by provider and is NOT guarded; neither configured
raises; and a server-map URL is trusted even if it would fail the guard's private rules.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from oraclous_harness_runtime_service.domain.llm.openai_compatible import OpenAICompatibleClient
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionError,
    HarnessExecutionService,
)
from oraclous_ohm.signatures import TrustStore

pytestmark = [pytest.mark.unit, pytest.mark.byom]

_ORG = uuid.uuid4()


def _base_url(client: OpenAICompatibleClient) -> str:
    """The client's effective base URL, minus httpx's normalising trailing slash."""
    return str(client._client.base_url).rstrip("/")


class _FakeBroker:
    """Returns a fixed decrypted credential payload (like the real broker's resolve_credential)."""

    def __init__(self, payload: dict) -> None:  # noqa: ANN001
        self._payload = payload
        self.calls: list[str] = []

    async def resolve_credential(self, *, credential_id, organisation_id):  # noqa: ANN001, ANN202
        self.calls.append(credential_id)
        return dict(self._payload)


def _manifest(*, binding: str = "openrouter/anthropic/claude-3.5") -> SimpleNamespace:
    model = SimpleNamespace(
        binding=binding,
        protocol_shape="openai-compatible",
        config={"credential_id": "cred-1"},
    )
    return SimpleNamespace(primary_model=lambda: model)


def _service(
    *, broker: _FakeBroker, base_urls: dict[str, str], allow_private: bool = True
) -> HarnessExecutionService:
    return HarnessExecutionService(
        registry=None,
        broker=broker,
        executions=None,
        assignments=None,
        checkpoints=None,
        provenance=None,
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="live",
        llm_base_urls=base_urls,
        llm_timeout=1.0,
        llm_allow_private=allow_private,
        max_iterations=6,
    )


async def test_connection_base_url_is_used_over_server_map() -> None:
    # An IP literal keeps the guard's check hermetic (no DNS); 8.8.8.8 is public, so it passes.
    broker = _FakeBroker({"api_key": "k", "base_url": "https://8.8.8.8/v1"})
    svc = _service(broker=broker, base_urls={"openrouter": "https://openrouter.ai/api/v1"})
    client = await svc._build_llm(_manifest(), _ORG)
    assert isinstance(client, OpenAICompatibleClient)
    assert _base_url(client) == "https://8.8.8.8/v1"
    await client.aclose()


async def test_falls_back_to_server_map_when_no_connection_base_url() -> None:
    broker = _FakeBroker({"api_key": "k"})  # no base_url on the connection
    svc = _service(broker=broker, base_urls={"openrouter": "https://openrouter.ai/api/v1"})
    client = await svc._build_llm(_manifest(), _ORG)
    assert _base_url(client) == "https://openrouter.ai/api/v1"
    await client.aclose()


async def test_raises_when_neither_connection_nor_server_map_has_a_base_url() -> None:
    broker = _FakeBroker({"api_key": "k"})  # no base_url
    svc = _service(broker=broker, base_urls={})  # provider not in the map
    with pytest.raises(HarnessExecutionError, match="no base URL for provider"):
        await svc._build_llm(_manifest(), _ORG)


async def test_connection_base_url_is_guarded_and_blocks_metadata() -> None:
    # 169.254.169.254 is the cloud-metadata endpoint — ALWAYS blocked, even with allow_private=True.
    broker = _FakeBroker({"api_key": "k", "base_url": "http://169.254.169.254/v1"})
    svc = _service(broker=broker, base_urls={}, allow_private=True)
    with pytest.raises(HarnessExecutionError, match="connection base_url rejected"):
        await svc._build_llm(_manifest(), _ORG)


async def test_connection_base_url_blocked_private_when_multi_tenant() -> None:
    broker = _FakeBroker({"api_key": "k", "base_url": "http://127.0.0.1:11434/v1"})
    svc = _service(broker=broker, base_urls={}, allow_private=False)
    with pytest.raises(HarnessExecutionError, match="connection base_url rejected"):
        await svc._build_llm(_manifest(), _ORG)


async def test_connection_base_url_private_allowed_single_tenant() -> None:
    broker = _FakeBroker({"api_key": "k", "base_url": "http://127.0.0.1:11434/v1"})
    svc = _service(broker=broker, base_urls={}, allow_private=True)
    client = await svc._build_llm(_manifest(), _ORG)
    assert _base_url(client) == "http://127.0.0.1:11434/v1"
    await client.aclose()


async def test_server_map_url_is_trusted_not_guarded() -> None:
    # A loopback server-map URL would fail the guard's multi-tenant rules — but operator-configured
    # URLs are TRUSTED and never guarded, so this builds even with allow_private=False.
    broker = _FakeBroker({"api_key": "k"})  # no connection base_url → server map is used
    svc = _service(
        broker=broker,
        base_urls={"openrouter": "http://127.0.0.1:9000/v1"},
        allow_private=False,
    )
    client = await svc._build_llm(_manifest(), _ORG)
    assert _base_url(client) == "http://127.0.0.1:9000/v1"
    await client.aclose()
