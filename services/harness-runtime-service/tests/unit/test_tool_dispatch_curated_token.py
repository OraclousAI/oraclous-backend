"""#1111 item 2 — the harness keeps the registry's curated tool-failure token.

Decision 2 (posted on #1111): tool errors are classified from the registry's curated
``error_type`` token. ``429``/``5xx``/timeout/connection-reset are transient (a later commit
retries them with the existing backoff helper); ``401``/``403`` (auth-failed) and a spent quota
fail the member at once. This commit pins only the CLASSIFICATION surface — the exception
``dispatch()`` raises for a failed tool call — never the retry-loop behaviour itself, which is a
separate commit against ``tool_use.py``.

The gap, in ``harness_execution_service.py``'s dispatch closure (~:1407-1442)::

    execution = await self._registry.execute(instance_id, payload)
    if execution.get("status") != "SUCCESS":
        detail = execution.get("error_message") or execution.get("status")
        raise RegistryError(f"tool execution failed: {detail}")

``execution.get("error_type")`` is read nowhere here — a curated ``PROVIDER_RATE_LIMITED`` /
``PROVIDER_QUOTA_EXHAUSTED`` / ``PROVIDER_AUTH_FAILED`` (curated by the capability-registry's
``classify_provider_status``, ``search_providers.py:100-135``, and threaded onto
``ExecutionResult.error_type`` before the execute response ever reaches this line) is thrown away
and every tool failure reaches the model as the same bare, unclassified ``RegistryError``. The
registry's OWN ``error_code`` field (a different, HTTP-body-level token — see
``test_registry_error_code.py``) already crosses this boundary; ``error_type`` does not, and
nothing crosses AT ALL when the registry call itself never completes (a ``5xx`` from the registry,
a timeout, a reset connection) — today that either raises a bare, unclassified ``RegistryError``
(a non-2xx status) or lets a raw ``httpx`` transport exception straight through (a timeout or a
reset, since ``RegistryClient.execute`` catches neither).

Pinned shape (mirrors the existing ``LLMClientError.transient`` convention in
``domain/llm/openai_compatible.py``, the sibling classifier for a transient LLM-call failure):
whatever ``dispatch()`` raises for a failed tool call is a ``RegistryError`` carrying

* ``error_code`` — the curated token, passed through unchanged when the registry supplied one
  (``RegistryError`` already has this slot; #692 already uses it for the HTTP-body ``error_code``,
  this reuses the same field for the execution-result ``error_type``, since only one of the two is
  ever present on a given failure);
* ``transient`` — ``True`` for rate-limited / any registry-call transport failure (5xx, timeout,
  connection reset), ``False`` for auth-failed / quota-exhausted.

RED until the [impl] lands. Out of scope here: the retry/backoff loop and the fail-fast member
error itself (later commit against ``tool_use.py``); the member-facing ``tool_quota_exhausted``
token (decision 3, a different, harness-owned vocabulary assigned above this layer).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.policy import resolve_policy_set
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_harness_runtime_service.services.registry_client import RegistryClient, RegistryError
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()

# A single-operation web-search-shaped reader — synthetic, the same abstraction level as
# ``test_operation_override_dispatch.py``'s "GitHub Reader" fake descriptor, not a real connector.
_DESCRIPTOR = {
    "id": "cap-web",
    "kind": "tool",
    "metadata": {"name": "Web Search"},
    "spec": {
        "type": "API",
        "capabilities": [
            {"name": "search", "description": "Search the web", "parameters": {"query": "str"}},
        ],
    },
}


def _manifest() -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref="core/web-search@1.0.0", binding="web")],
        runtime=OHMRuntime(entrypoint="web"),
    )


Dispatch = Callable[[ToolSpec, dict[str, Any]], Awaitable[dict[str, Any]]]


class _FakeProvenance:
    """A real recording double instead of ``None`` against a non-optional ``provenance``
    parameter — this test never inspects emissions (precedent: #826 cleanup)."""

    async def emit(self, record: Any) -> None:  # noqa: ANN401
        return None


async def _runnable(registry: Any) -> tuple[Dispatch, dict[str, ToolSpec]]:
    """The REAL dispatch closure ``_build_runnable`` hands the loop, over a canned resolution —
    same shape as ``test_operation_override_dispatch.py``'s ``_runnable``."""
    service = HarnessExecutionService(
        registry=registry,
        broker=None,
        executions=None,
        assignments=None,
        checkpoints=None,
        provenance=_FakeProvenance(),
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=None,
    )

    async def _resolve_all(_manifest: Any) -> dict[str, dict[str, Any]]:  # noqa: ANN401
        return {"web": {"id": "cap-web", "name": "Web Search", "descriptor": _DESCRIPTOR}}

    async def _build_llm(_manifest: Any, _org_id: Any) -> Any:  # noqa: ANN401
        return object()

    service._resolve_all = _resolve_all  # type: ignore[method-assign]
    service._build_llm = _build_llm  # type: ignore[method-assign]
    _, tool_specs, dispatch, _, _ = await service._build_runnable(
        _manifest(), resolve_policy_set(None), _ORG
    )
    return dispatch, {s.name: s for s in tool_specs}


# --- a business-level tool failure: the registry call completes, ``status`` says FAILED ----------


class _Registry:
    """Materialisation is a no-op; ``execute`` returns the canned execution result."""

    def __init__(self, execution: dict[str, Any]) -> None:
        self._execution = execution

    async def list_instances(self) -> list[dict[str, Any]]:
        return []

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        return {}

    async def execute(self, instance_id: uuid.UUID, input_data: dict[str, Any]) -> dict[str, Any]:
        return self._execution


@pytest.mark.parametrize(
    ("error_type", "expected_transient"),
    [
        ("PROVIDER_RATE_LIMITED", True),
        ("PROVIDER_QUOTA_EXHAUSTED", False),
        ("PROVIDER_AUTH_FAILED", False),
    ],
)
async def test_the_curated_provider_token_is_carried_with_its_transience(
    error_type: str, expected_transient: bool
) -> None:
    registry = _Registry(
        {"status": "FAILED", "error_message": "the tool call failed", "error_type": error_type}
    )
    dispatch, specs = await _runnable(registry)

    with pytest.raises(RegistryError) as ei:
        await dispatch(specs["web__search"], {"query": "interest rates"})

    assert ei.value.error_code == error_type, (
        "the registry's curated error_type was dropped rather than carried onto the raised error"
    )
    assert ei.value.transient is expected_transient


async def test_a_curated_token_is_never_silently_treated_as_success() -> None:
    """The regression guard behind the parametrised cases: a FAILED execution with a curated
    token still raises — carrying the token is never mistaken for handling the failure."""
    registry = _Registry(
        {
            "status": "FAILED",
            "error_message": "quota exhausted",
            "error_type": "PROVIDER_QUOTA_EXHAUSTED",
        }
    )
    dispatch, specs = await _runnable(registry)

    with pytest.raises(RegistryError):
        await dispatch(specs["web__search"], {"query": "interest rates"})


# --- a transport-level failure: the registry call itself never comes back SUCCESS or FAILED ------


def _registry_over_transport(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    """A fake registry whose ``execute`` is the REAL ``RegistryClient.execute`` over a stubbed
    transport, so a transport-level failure of the call to the registry itself (5xx, timeout,
    connection reset) reaches ``dispatch()`` exactly as ``RegistryClient`` actually surfaces it —
    not a hand-built stand-in exception."""
    client = RegistryClient("http://registry", headers={}, transport=httpx.MockTransport(handler))

    class _Delegating:
        async def list_instances(self) -> list[dict[str, Any]]:
            return []

        async def create_instance(
            self, *, capability_id: str, name: str, configuration: dict[str, Any]
        ) -> dict[str, Any]:
            return {"id": str(uuid.uuid4())}

        async def configure_credentials(
            self, instance_id: uuid.UUID, mappings: dict[str, str]
        ) -> dict[str, Any]:
            return {}

        async def execute(
            self, instance_id: uuid.UUID, input_data: dict[str, Any]
        ) -> dict[str, Any]:
            return await client.execute(instance_id, input_data)

    return _Delegating()


async def test_a_5xx_from_the_registry_call_itself_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(503, json={"detail": "registry temporarily unavailable"})

    dispatch, specs = await _runnable(_registry_over_transport(handler))

    with pytest.raises(RegistryError) as ei:
        await dispatch(specs["web__search"], {"query": "interest rates"})

    assert ei.value.transient is True


async def test_a_registry_call_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out waiting on the registry", request=request)

    dispatch, specs = await _runnable(_registry_over_transport(handler))

    with pytest.raises(RegistryError) as ei:
        await dispatch(specs["web__search"], {"query": "interest rates"})

    assert ei.value.transient is True


async def test_a_registry_connection_reset_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("Connection reset by peer", request=request)

    dispatch, specs = await _runnable(_registry_over_transport(handler))

    with pytest.raises(RegistryError) as ei:
        await dispatch(specs["web__search"], {"query": "interest rates"})

    assert ei.value.transient is True
