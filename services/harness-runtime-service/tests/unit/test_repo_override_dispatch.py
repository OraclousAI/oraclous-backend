"""Unit (#1047, Q1): at dispatch, the DISPATCHING INSTANCE's bound ``repo`` always wins — the
sink connector's own confused-deputy defence (``test_github_sink_connector.py``, capability-
registry-service), mirrored one layer up so a model-supplied override never reaches the registry
at all. Same shape as #956's ``operation`` binding, applied to ``repo`` instead.

Owner ruling (16 Sep, on #1047, Q1 + Q2):

* a model-supplied ``repo`` that differs from the one the dispatching instance binds is refused
  before the registry is ever called, with a coded error fed back to the model;
* a matching ``repo`` is stripped from the payload and the call proceeds — the connector already
  falls back to its own instance configuration for the value (``context.configuration["repo"]``),
  so nothing is lost by not re-adding it;
* ``repo`` left the model-facing ``deliver`` operation's parameters entirely (Q2), so this check
  cannot be schema-enforced the way an ordinary argument would be — it has to happen here, at
  dispatch, the same reason #956's ``operation`` check lives here and not only in the schema;
* the key is matched case- AND whitespace-insensitively, mirroring #1004's ``operation`` hardening
  (stricter than #1004 itself, which only folded case);
* a null ``repo`` is stripped, never treated as an override attempt (distinct from ``operation``,
  where ``None`` IS a mismatch — see ``test_dispatch_payload_hardening.py``'s non-string-operation
  cases). A repository name is more identifying than an operation name, so the refusal text must
  echo NEITHER the bound NOR the supplied repository — stricter than #956's own operation message,
  which does echo the bound operation.

``ToolSpec`` gains a ``bound_repo: str | None = None`` field (mirroring ``nullable_keys``): unset
for every tool built before this ticket, so an ordinary connector that genuinely takes ``repo`` as
a call argument (no bound instance repo) is completely unaffected — this ticket only ever fires
for a tool whose dispatching instance actually binds one.

RED until the #1047 ``[impl]`` lands: today ``ToolSpec`` has no ``bound_repo`` field and
``dispatch_payload`` has no notion of a bound repository at all.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.policy import resolve_policy_set
from oraclous_harness_runtime_service.domain.tool_schemas import dispatch_payload, tool_specs_for
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

#: The run page's per-step budget, the same bound the operation refusal (#956) and the mcp
#: connector's error text keep to.
_BOUNDED = 300

_BOUND_REPO = "OraclousAI/oraclous-backend"
_OTHER_REPO = "someone-else/private-repo"

_ORG = uuid.uuid4()


def _bound_spec(**overrides: Any) -> ToolSpec:
    """A github-sink-shaped ``deliver`` tool whose dispatching instance binds ``_BOUND_REPO``.

    ``repo`` is deliberately absent from ``parameters``/``properties`` (#1047 ruling Q2: it left
    the model-facing schema entirely) — this fixture only makes sense once ``ToolSpec`` carries the
    bound value out-of-band, exactly like ``nullable_keys`` already does for a different concern.
    """
    kwargs: dict[str, Any] = {
        "name": "sink__deliver",
        "description": "Write changed files to a head branch + open a PR",
        "parameters": {
            "type": "object",
            "properties": {
                "base_branch": {"type": "string"},
                "head_branch": {"type": "string"},
                "files": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["base_branch", "head_branch", "files"],
            "additionalProperties": False,
        },
        "binding": "sink",
        "operation": "deliver",
        "bound_repo": _BOUND_REPO,
    }
    kwargs.update(overrides)
    return ToolSpec(**kwargs)


def _unbound_spec() -> ToolSpec:
    """A tool whose instance binds NO repo at all — the regression guard: a connector where
    ``repo`` is a genuine, unprotected call argument (the ``GitHub Reader`` shape in
    ``test_operation_override_dispatch.py``) must be completely untouched by this ticket."""
    return ToolSpec(
        name="gh__read_file",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        binding="gh",
        operation="read_file",
    )


# --- ToolSpec carries the new field, defaulted off --------------------------------------------


def test_bound_repo_defaults_to_none() -> None:
    """Every ``ToolSpec`` built before this ticket omits ``bound_repo`` — it must not retroactively
    start enforcing anything for them."""
    spec = ToolSpec(name="x__op", description="d", parameters={}, binding="x", operation="op")
    assert spec.bound_repo is None


# --- tool_specs_for actually WIRES a bound repo onto the produced spec(s) ------------------------
#
# The gap this closes: every test above (and below) builds its ``ToolSpec`` BY HAND via
# ``_bound_spec()``, so nothing here pinned that ``tool_specs_for`` — the real call site (#911,
# ``harness_execution_service._materialise``) — ever sets ``bound_repo`` from the dispatching
# instance's ``bound_config``. An implementation could add the field (above) and never populate
# it here, and every refusal test in this file would still pass against a spec no production code
# path can actually produce. Mirrors ``test_tool_schemas.py``'s already-green schema-shape
# assertions (``repo`` excluded from ``properties``/``required``/``nullable_keys``) one concern
# over: THIS values ``ToolSpec.bound_repo`` itself, not the schema.

_TWO_OP_SINK_DESCRIPTOR: dict[str, Any] = {
    "id": "core-github-sink",
    "metadata": {"name": "GitHub Sink"},
    "spec": {
        "type": "API",
        "capabilities": [
            {
                "name": "deliver",
                "description": "Write changed files to a head branch + open a PR",
                "parameters": {"base_branch": "str", "head_branch": "str", "files": "list"},
            },
            {
                "name": "close_pr",
                "description": "Close an open PR without merging",
                "parameters": {"pr_number": "int"},
            },
        ],
        "input_schema": {
            "type": "object",
            "required": ["files"],
            "properties": {
                "base_branch": {"type": "string"},
                "head_branch": {"type": "string"},
                "files": {"type": "array", "items": {"type": "object"}},
                "pr_number": {"type": "integer"},
            },
        },
    },
}


def test_tool_specs_for_sets_bound_repo_on_every_produced_spec_from_bound_config() -> None:
    specs = tool_specs_for("sink", _TWO_OP_SINK_DESCRIPTOR, bound_config={"repo": _BOUND_REPO})
    assert len(specs) == 2
    assert {s.bound_repo for s in specs} == {_BOUND_REPO}


def test_tool_specs_for_leaves_bound_repo_none_with_no_bound_config_kwarg() -> None:
    specs = tool_specs_for("sink", _TWO_OP_SINK_DESCRIPTOR)
    assert all(s.bound_repo is None for s in specs)


def test_tool_specs_for_leaves_bound_repo_none_when_bound_config_is_none() -> None:
    specs = tool_specs_for("sink", _TWO_OP_SINK_DESCRIPTOR, bound_config=None)
    assert all(s.bound_repo is None for s in specs)


def test_tool_specs_for_leaves_bound_repo_none_when_bound_config_has_no_repo_key() -> None:
    specs = tool_specs_for("sink", _TWO_OP_SINK_DESCRIPTOR, bound_config={"graph_id": "some-uuid"})
    assert all(s.bound_repo is None for s in specs)


# --- a call naming no repo at all, or a tool with no bound repo, is untouched -------------------


def test_a_call_naming_no_repo_at_all_is_unaffected() -> None:
    payload = dispatch_payload(
        _bound_spec(), {"base_branch": "main", "head_branch": "x", "files": []}
    )
    assert payload == {
        "operation": "deliver",
        "base_branch": "main",
        "head_branch": "x",
        "files": [],
    }


def test_a_tool_with_no_bound_repo_still_passes_repo_through_untouched() -> None:
    payload = dispatch_payload(_unbound_spec(), {"repo": _OTHER_REPO})
    assert payload == {"operation": "read_file", "repo": _OTHER_REPO}


# --- a differing repo fails closed, before anything is dispatched -------------------------------


def test_a_model_supplied_repo_that_differs_is_refused() -> None:
    from oraclous_harness_runtime_service.domain.tool_schemas import RepoOverrideRefused

    with pytest.raises(RepoOverrideRefused):
        dispatch_payload(
            _bound_spec(),
            {"repo": _OTHER_REPO, "base_branch": "main", "head_branch": "x", "files": []},
        )


def test_the_refusal_is_coded_and_bounded_and_echoes_neither_repo_name() -> None:
    from oraclous_harness_runtime_service.domain.tool_schemas import (
        REPO_OVERRIDE_REFUSED,
        RepoOverrideRefused,
    )

    with pytest.raises(RepoOverrideRefused) as ei:
        dispatch_payload(_bound_spec(), {"repo": _OTHER_REPO, "files": []})

    detail = str(ei.value)
    assert REPO_OVERRIDE_REFUSED in detail
    assert _OTHER_REPO not in detail, "the refusal echoes the model-supplied repository name"
    assert _BOUND_REPO not in detail, "the refusal echoes the instance-bound repository name"
    assert len(detail) <= _BOUNDED


@pytest.mark.parametrize("value", [["a/b"], {"name": "a/b"}, 1, True])
def test_a_non_string_repo_value_that_differs_is_a_mismatch_too(value: object) -> None:
    """Present-and-not-equal is the whole test; a type the key cannot legitimately hold is not a
    loophole into "absent" — ``None`` is the one deliberate exception (see the null test below)."""
    from oraclous_harness_runtime_service.domain.tool_schemas import RepoOverrideRefused

    with pytest.raises(RepoOverrideRefused):
        dispatch_payload(_bound_spec(), {"repo": value, "files": []})


def test_a_null_repo_is_stripped_not_refused() -> None:
    """Distinct from ``operation`` (where ``None`` IS a mismatch): a null value on a bound-but-
    unsupplied key is noise to clean up, not an override attempt (#898's spirit, pinned here for
    ``repo`` specifically since ``repo`` is no longer a projected, nullable-widened schema key at
    all post ruling Q2)."""
    payload = dispatch_payload(
        _bound_spec(), {"repo": None, "base_branch": "main", "head_branch": "x", "files": []}
    )
    assert "repo" not in payload
    assert payload == {
        "operation": "deliver",
        "base_branch": "main",
        "head_branch": "x",
        "files": [],
    }


# --- a matching repo is stripped and the call proceeds -------------------------------------------


def test_a_matching_repo_is_stripped_and_the_call_proceeds() -> None:
    """The connector already falls back to ``context.configuration["repo"]``, so dropping a
    matching value here costs nothing — the registry sees the bound key exactly zero times, never
    twice."""
    payload = dispatch_payload(
        _bound_spec(),
        {"repo": _BOUND_REPO, "base_branch": "main", "head_branch": "x", "files": []},
    )
    assert "repo" not in payload
    assert payload == {
        "operation": "deliver",
        "base_branch": "main",
        "head_branch": "x",
        "files": [],
    }


# --- case / whitespace variants of the key cannot sneak a differing repo through -----------------


@pytest.mark.parametrize("key", ["Repo", "REPO", " repo", "repo ", "\trepo\n"])
def test_a_case_or_whitespace_variant_holding_a_different_repo_is_refused(key: str) -> None:
    from oraclous_harness_runtime_service.domain.tool_schemas import RepoOverrideRefused

    with pytest.raises(RepoOverrideRefused):
        dispatch_payload(_bound_spec(), {key: _OTHER_REPO, "files": []})


@pytest.mark.parametrize("key", ["Repo", "REPO", " repo", "repo "])
def test_a_case_or_whitespace_variant_holding_the_bound_repo_is_stripped(key: str) -> None:
    """Same rule as the exact-case key: naming the repo you were in fact given changes nothing."""
    payload = dispatch_payload(_bound_spec(), {key: _BOUND_REPO, "files": []})

    assert key not in payload
    assert "repo" not in payload
    assert payload == {"operation": "deliver", "files": []}


def test_two_spellings_that_disagree_are_refused() -> None:
    """Fail-closed: a payload carrying two different answers to "which repo" is refused, never
    resolved by ordering — mirrors #1004's ``operation`` hardening."""
    from oraclous_harness_runtime_service.domain.tool_schemas import RepoOverrideRefused

    with pytest.raises(RepoOverrideRefused):
        dispatch_payload(_bound_spec(), {"repo": _BOUND_REPO, "Repo": _OTHER_REPO, "files": []})


def test_a_nested_repo_key_is_the_tools_own_argument() -> None:
    """The strip stays SHALLOW (#698 D3, #956 precedent): a nested ``repo`` belongs to the tool's
    own input and travels intact — here, inside one ``files`` array element."""
    nested = {"files": [{"repo": _OTHER_REPO, "path": "x"}]}

    payload = dispatch_payload(_bound_spec(), nested)

    assert payload == {"operation": "deliver", **nested}


# --- the refusal is the same kind of event as the operation one, for the dispatch's catch --------


def test_the_repo_override_refusal_shares_the_same_base() -> None:
    """The dispatch closure in ``harness_execution_service.py`` catches ``ToolDispatchRefused``
    generically (besides its own specific ``OperationOverrideRefused`` branch) —
    ``RepoOverrideRefused`` must be caught the same way, with no new catch clause required."""
    from oraclous_harness_runtime_service.domain.tool_schemas import (
        RepoOverrideRefused,
        ToolDispatchRefused,
    )

    assert issubclass(RepoOverrideRefused, ToolDispatchRefused)


# --- the wiring holds end to end: a REAL _materialise + the REAL dispatch closure ---------------


class _RecordingRegistry:
    """Records every ``execute`` payload. Copied verbatim (shape-for-shape) from
    ``test_operation_override_dispatch.py``'s ``_Registry`` — the file this test mirrors one layer
    up (``repo`` instead of ``operation``)."""

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


class _FakeProvenance:
    """Copied verbatim from ``test_operation_override_dispatch.py``: a real recording double
    instead of ``None`` against a non-optional ``provenance: ProvenanceCollector`` parameter."""

    async def emit(self, record: Any) -> None:
        return None


async def test_a_materialised_sink_instance_refuses_a_call_naming_a_different_repo() -> None:
    """Dispatch-closure level companion to the ``tool_specs_for`` tests above: a github-sink
    capability whose manifest ``config`` binds ``_BOUND_REPO`` is freshly minted by the REAL
    ``_materialise`` (the ``else``/fresh-mint branch — ``cap.config`` becomes the instance's
    ``configuration`` and reaches ``tool_specs_for`` as ``bound_config``, #911), so the produced
    ``ToolSpec.bound_repo`` is ``_BOUND_REPO`` with no test-built ``ToolSpec`` anywhere in this
    test. A model call naming a DIFFERENT repo must be refused by the REAL dispatch closure
    ``_build_runnable`` hands the loop — the registry client is never called and the error is a
    coded ``RepoOverrideRefused`` — mirroring
    ``test_operation_override_dispatch.py``'s
    ``test_a_model_supplied_operation_that_differs_never_reaches_the_registry`` one layer up.

    RED for the same compounding reasons as the rest of this file: no ``bound_repo`` field, no
    ``tool_specs_for`` wiring, and no ``dispatch_payload`` refusal exist yet."""
    registry = _RecordingRegistry()
    manifest = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[
            OHMCapability(
                ref="core/github-sink@1.0.0", binding="sink", config={"repo": _BOUND_REPO}
            )
        ],
        runtime=OHMRuntime(entrypoint="sink"),
    )
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
        return {
            "sink": {"id": _TWO_OP_SINK_DESCRIPTOR["id"], "descriptor": _TWO_OP_SINK_DESCRIPTOR}
        }

    async def _build_llm(_manifest: Any, _org_id: Any) -> Any:  # noqa: ANN401
        return object()

    service._resolve_all = _resolve_all  # type: ignore[method-assign]
    service._build_llm = _build_llm  # type: ignore[method-assign]
    _, tool_specs, dispatch, _, _ = await service._build_runnable(
        manifest, resolve_policy_set(None), _ORG
    )
    specs = {s.name: s for s in tool_specs}
    assert specs["sink__deliver"].bound_repo == _BOUND_REPO

    from oraclous_harness_runtime_service.domain.tool_schemas import RepoOverrideRefused

    with pytest.raises(RepoOverrideRefused):
        await dispatch(
            specs["sink__deliver"],
            {"repo": _OTHER_REPO, "base_branch": "main", "head_branch": "x", "files": []},
        )

    assert registry.executed == [], "the override reached the registry — CLAUDE.md §3.5 fail-closed"
