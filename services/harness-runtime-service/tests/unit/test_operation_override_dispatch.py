"""Unit (#956, ruling 1): at dispatch the bound ``spec.operation`` always wins.

The defect, in one line of ``harness_execution_service``'s dispatch closure::

    await self._registry.execute(instance_id, {"operation": spec.operation, **args})

``**args`` spreads AFTER the literal key, so a model that puts ``operation`` in its function-call
arguments picks which operation the connector runs — the binding the runtime made is advisory.
``args`` is the raw ``json.loads`` of the model's output, and nothing between the model and this
line rejects the extra key.

Ruled by the owner (recorded on #956):

* the model-supplied ``operation`` key is stripped before dispatch;
* if it was present AND differs from the bound operation, the call fails CLOSED with a coded,
  bounded error fed back to the model — a mismatch is never silently ignored;
* the mismatch is logged at WARNING without echoing the supplied value beyond 64 characters.

These tests drive the REAL dispatch closure ``_build_runnable`` hands the loop, over a recording
registry, so they pin the site named in the ruling rather than a helper that may or may not be on
the path. Precedent for reaching the closure this way: ``test_web_search_trust_predicate.py``.

RED until the #956 ``[impl]`` lands: today the override reaches the registry.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.policy import resolve_policy_set
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

_ORG = uuid.uuid4()

#: The code the refusal carries — a closed-vocabulary token the model can act on, the way #692's
#: registry codes are. ``str(exc)`` is what the loop feeds back as ``detail``.
_CODE = "operation_override_refused"

#: The run page's per-step budget, the same bound the mcp connector caps a tool's error text at.
_BOUNDED = 300

# A first-party reader exposing exactly two read operations. ``delete_repo`` is deliberately NOT
# declared — it stands for any operation the connector may implement that this binding never
# offered the model.
_DESCRIPTOR = {
    "id": "cap-gh",
    "kind": "tool",
    "metadata": {"name": "GitHub Reader"},
    "spec": {
        "type": "API",
        "capabilities": [
            {"name": "read_file", "description": "Read a file", "parameters": {"repo": "str"}},
            {"name": "list_files", "description": "List files", "parameters": {"repo": "str"}},
        ],
    },
}


class _Registry:
    """Records every ``execute`` payload — the registry's view of what the model asked for."""

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

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
        self.executed.append(dict(input_data))
        return {"status": "SUCCESS", "output_data": {"ok": True}}


def _manifest() -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref="core/github-reader@1.0.0", binding="gh")],
        runtime=OHMRuntime(entrypoint="gh"),
    )


Dispatch = Callable[[ToolSpec, dict[str, Any]], Awaitable[dict[str, Any]]]


class _FakeProvenance:
    """#826 cleanup: a real recording double instead of `None` against a non-optional
    ``provenance: ProvenanceCollector`` parameter — this test never inspects emissions."""

    async def emit(self, record: Any) -> None:
        return None


async def _runnable(registry: _Registry) -> tuple[Dispatch, dict[str, ToolSpec]]:
    """The REAL dispatch closure + the specs the model is offered, over a canned resolution."""
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
        return {"gh": {"id": "cap-gh", "name": "GitHub Reader", "descriptor": _DESCRIPTOR}}

    async def _build_llm(_manifest: Any, _org_id: Any) -> Any:  # noqa: ANN401
        return object()

    service._resolve_all = _resolve_all  # type: ignore[method-assign]
    service._build_llm = _build_llm  # type: ignore[method-assign]
    _, tool_specs, dispatch, _, _ = await service._build_runnable(
        _manifest(), resolve_policy_set(None), _ORG
    )
    return dispatch, {s.name: s for s in tool_specs}


# --- the normal call is untouched ---------------------------------------------------------------


async def test_a_plain_call_dispatches_the_bound_operation() -> None:
    """The regression guard: a call with no ``operation`` key is exactly what it was before."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)

    result = await dispatch(specs["gh__read_file"], {"repo": "OraclousAI/oraclous-backend"})

    assert result == {"ok": True}
    assert registry.executed == [{"operation": "read_file", "repo": "OraclousAI/oraclous-backend"}]


# --- a differing operation fails closed, before the registry -------------------------------------


async def test_a_model_supplied_operation_that_differs_never_reaches_the_registry() -> None:
    """The defect itself. Today the registry receives ``operation == "delete_repo"``."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)

    with pytest.raises(Exception):  # noqa: B017, PT011 — the class is the implementer's; the contract is "raises, dispatches nothing"
        await dispatch(specs["gh__read_file"], {"operation": "delete_repo", "repo": "a/b"})

    assert registry.executed == [], "the override reached the registry — CLAUDE.md §3.5 fail-closed"


async def test_the_refusal_is_coded_and_bounded() -> None:
    """``str(exc)`` is the ``detail`` the loop feeds back to the model (``tool_use.py``'s dispatch
    ``except``), so it must carry a code the model can act on and must not become an echo
    channel for whatever the model put in the key."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)
    injected = "x" * 2000

    with pytest.raises(Exception) as ei:  # noqa: B017, PT011
        await dispatch(specs["gh__read_file"], {"operation": injected, "repo": "a/b"})

    detail = str(ei.value)
    assert _CODE in detail
    assert injected not in detail
    assert len(detail) <= _BOUNDED


async def test_an_operation_the_binding_does_offer_is_still_refused_on_the_wrong_tool() -> None:
    """``list_files`` IS a declared operation — but the model called ``gh__read_file``. Which
    operation runs is decided by which tool was called, never by an argument."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)

    with pytest.raises(Exception):  # noqa: B017, PT011
        await dispatch(specs["gh__read_file"], {"operation": "list_files", "repo": "a/b"})

    assert registry.executed == []


async def test_the_match_is_exact_not_case_folded() -> None:
    """Fail-closed means no normalisation: ``READ_FILE`` is not ``read_file``."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)

    with pytest.raises(Exception):  # noqa: B017, PT011
        await dispatch(specs["gh__read_file"], {"operation": "READ_FILE", "repo": "a/b"})

    assert registry.executed == []


@pytest.mark.parametrize("value", [None, ["read_file"], {"name": "read_file"}, 1])
async def test_a_non_string_operation_value_is_a_mismatch_too(value: object) -> None:
    """Present-and-not-equal is the whole test; a type the key cannot legitimately hold is not
    a loophole into "absent"."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)

    with pytest.raises(Exception):  # noqa: B017, PT011
        await dispatch(specs["gh__read_file"], {"operation": value, "repo": "a/b"})

    assert registry.executed == []


# --- a matching operation is stripped and the call proceeds ---------------------------------------


async def test_a_matching_operation_key_is_stripped_and_the_call_proceeds() -> None:
    """A model that names the operation it was in fact given has changed nothing; refusing it
    would cost a turn for no security gain. The registry sees the bound key exactly once."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)

    await dispatch(specs["gh__read_file"], {"operation": "read_file", "repo": "a/b"})

    assert registry.executed == [{"operation": "read_file", "repo": "a/b"}]


async def test_a_nested_operation_key_is_the_tools_own_argument() -> None:
    """The strip is SHALLOW, matching the mcp connector's ``_arguments`` (#698 D3): a nested
    ``operation`` belongs to the tool's own input and travels intact."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)
    nested = {"repo": "a/b", "filters": {"operation": "delete_repo", "paths": ["x"]}}

    await dispatch(specs["gh__read_file"], nested)

    assert registry.executed == [{"operation": "read_file", **nested}]


# --- the mismatch is logged, bounded ------------------------------------------------------------


async def test_the_mismatch_is_logged_at_warning_without_echoing_past_64_chars(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator must be able to see an override attempt (it is a signal about the model, or
    about a prompt injection), and the log must not become the echo channel the error message
    was just closed as."""
    registry = _Registry()
    dispatch, specs = await _runnable(registry)
    injected = "".join(chr(ord("a") + i % 26) for i in range(500))

    with caplog.at_level(logging.WARNING), pytest.raises(Exception):  # noqa: B017, PT011
        await dispatch(specs["gh__read_file"], {"operation": injected, "repo": "a/b"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no WARNING was logged for an operation override attempt"
    texts = [r.getMessage() for r in warnings]
    assert any("gh__read_file" in t or "gh" in t for t in texts), (
        "the WARNING does not name the tool the override was attempted on"
    )
    assert all(injected[:65] not in t for t in texts), (
        "the WARNING echoes more than 64 characters of the model-supplied value"
    )
