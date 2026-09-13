"""Unit: the new ``draft-manifest`` connector (#900, ADR-053 decisions 1-3) — the manifest-
drafter's tool call IS its structured answer.

Shaped EXACTLY like ``ManifestValidateConnector`` (#594/#705/ADR-047) — see
``test_manifest_validate_connector.py``, this file's precise template. Same fail-closed reads-the-
registry-itself contract, same builtins-only floor with no repository, same fail-closed-on-
exception verdict. ``DraftManifestConnector``/``DraftManifestPlugin`` do not exist anywhere on
``main`` yet — every test below imports them FUNCTION-LOCALLY (``.claude/rules/tests-seam-imports.
md``), never at module level, so this file collects cleanly and fails at RUN time with a clean
``ModuleNotFoundError`` until ``[impl]`` lands.

── THE DESIGN DECISION THIS FILE MAKES, EXPLICITLY (flagged in the #900 [tests] report) ──────────

Unlike ``manifest-validate`` (whose tool-call shape is the fixed wrapper ``{"draft": <object or
text>}``, because the REVIEWER relays a manifest it did not author), ``draft-manifest`` is the
member the model-authored manifest COMES FROM: ADR-053 decision 2 makes the manifest-drafter's own
tool-call ARGUMENTS its structured answer (``run_tool_use_loop`` — see
``services/harness-runtime-service/tests/unit/test_answer_from_tool_loop.py::
test_a_successful_call_to_the_named_tool_ends_the_loop_and_becomes_the_answer`` — turns
``ToolCall.args`` straight into ``result.output``, byte for byte, never the connector's own
returned ``data``). A per-run ``resolved_schema`` (ADR-053 decision 1, pinned separately in
``packages/ohm/tests/compiler/test_team.py``) then constrains those TOP-LEVEL call arguments to
look like a drafted OHM Team Harness (``members``, etc.) — that per-run schema story is only
coherent if the tool's own arguments ARE the drafted team, not a value nested one level down under
a ``"draft"`` key nobody would then be constraining. ``oraclous_ohm.compiler.validate_draft``'s own
signature (``draft: str | dict[str, Any]``) already accepts either shape, so passing ``input_data``
straight through costs nothing and needs no unwrapping step.

CONCLUSION PINNED HERE: ``DraftManifestConnector._execute_internal``'s ``input_data`` dict IS the
drafted manifest directly — ``validate_draft(input_data, catalog, …)``, never
``input_data.get("draft")``. The sibling's own "the draft arrives as the reviewer's relayed ```json-
fenced TEXT" test has NO direct analog here and is deliberately NOT mirrored: ``InternalTool.
execute()`` (``domain/executors/base.py``) already refuses non-dict ``input_data`` before
``_execute_internal`` ever runs (a real tool call's arguments are always a JSON object per the
provider's own tool-calling protocol — there is no route by which this connector's ``input_data``
is ever a bare string at the top level, unlike ``manifest-validate``'s caller-relayed field). One
test below (``test_a_non_dict_top_level_call_is_rejected_before_it_ever_reaches_this_connector``)
pins that structural difference explicitly rather than silently dropping the coverage.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.models.enums import DescriptorKind

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
#: an imported, approved MCP tool — a descriptor ROW, so the in-process plugin registry cannot see
#: it. The same #705 shape the sibling connector's regression pin exercises.
_IMPORTED = "github-mcp-add-issue-comment"
#: a genuinely registered built-in (a plugin class compiled into the service)
_BUILTIN = "web-research"


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
    )


def _draft(tool: str) -> dict:
    """The drafted team — passed AS ``input_data`` directly (see module docstring): this is the
    tool call's own arguments, not a value nested under a ``"draft"`` key."""
    return {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/r@1",
                "tools": [tool],
                "tool_rationale": {tool: f"needs {tool} to cover this sub-goal"},  # #718
                "outputs_schema": {"required": ["summary"]},  # #697
            },
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
                "outputs_schema": {"required": ["summary"]},  # #697
            },
        ]
    }


def _row(
    name: str, *, status: str = "active", kind: DescriptorKind = DescriptorKind.TOOL
) -> SimpleNamespace:
    """A registered capability descriptor row as the repository returns it."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        name=name,
        status=status,
        kind=kind,
        descriptor={"kind": str(kind), "metadata": {"name": name}},
    )


class _FakeCapabilityRepo:
    """Stands in for ``CapabilityRepository`` — the org's registered descriptor rows. Copied
    verbatim from ``test_manifest_validate_connector.py``'s own fixture (both org-scoped read
    methods offered, so these tests pin BEHAVIOUR and leave the query-vs-connector filter choice
    to ``[impl]``)."""

    def __init__(self, rows: list[SimpleNamespace], *, explode: bool = False) -> None:
        self._rows = rows
        self._explode = explode
        self.orgs: list[uuid.UUID] = []

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[SimpleNamespace]:
        self.orgs.append(organisation_id)
        if self._explode:
            raise RuntimeError("registry read failed")
        return list(self._rows)

    async def list_by_kind(
        self, organisation_id: uuid.UUID, kind: DescriptorKind
    ) -> list[SimpleNamespace]:
        rows = await self.list_by_org(organisation_id)
        return [r for r in rows if r.kind == kind]


def _connector(repo: _FakeCapabilityRepo | None) -> Any:
    """Function-local import: ``draft_manifest`` is a not-yet-built #900 seam."""
    from oraclous_capability_registry_service.domain.connectors.draft_manifest import (
        DraftManifestConnector,
    )

    ex = DraftManifestConnector({"id": "x"})
    if repo is not None:
        ex.capability_repo = repo  # injected by ToolExecutionService on the live path
    return ex


async def test_a_clean_draft_passes() -> None:
    ex = _connector(_FakeCapabilityRepo([_row("web-search")]))
    res = await ex.execute(_draft("web-search"), _ctx())
    assert res.success is True
    assert res.data["would_block"] is False


async def test_a_hallucinated_tool_blocks_fail_closed() -> None:
    ex = _connector(_FakeCapabilityRepo([_row("web-search")]))
    res = await ex.execute(_draft("teleport"), _ctx())
    assert res.success is True  # the validation RAN (would_block is data, not a tool failure)
    assert res.data["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.data["blocking"])


async def test_a_non_dict_top_level_call_is_rejected_before_it_ever_reaches_this_connector() -> (
    None
):
    """Structural difference from ``manifest-validate`` (see module docstring): a real tool call's
    arguments are always a JSON object, so ``InternalTool.execute()`` refuses a non-dict
    ``input_data`` generically, before ``_execute_internal`` runs at all — there is no "the draft
    arrived as prose" path to fall back into for THIS connector the way there is for the reviewer's
    caller-relayed field."""
    ex = _connector(_FakeCapabilityRepo([_row("web-search")]))
    res = await ex.execute("not a JSON object", _ctx())  # type: ignore[arg-type]
    assert res.success is False
    assert res.error_type == "INVALID_INPUT"


async def test_an_imported_mcp_tool_passes_with_no_relayed_catalog() -> None:
    """#705's rule, inherited: an imported, approved MCP tool is a descriptor row the in-process
    plugin registry cannot see. With no relay possible at all (there is no catalog ARGUMENT to
    relay — the whole payload is the drafted team), the gate must still admit it, sourced from the
    org's registry."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED)])
    res = await _connector(repo).execute(_draft(_IMPORTED), _ctx())
    assert res.success is True
    assert res.data["would_block"] is False
    assert repo.orgs == [_ORG]  # org-scoped: the gate reads the CALLER's registry (ADR-006)


async def test_an_extra_key_beside_the_drafted_team_cannot_widen_the_allowed_set() -> None:
    """No wrapper key means no relay vector — but a model could still stuff an extra top-level key
    into its call (whether or not the strict per-run schema — #898 — would have allowed it). The
    verdict must be identical whether or not one is present, because the catalog is read, never
    read FROM the call."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED), _row("web-search")])
    plain = await _connector(repo).execute(_draft(_IMPORTED), _ctx())
    with_extra = await _connector(repo).execute(
        {**_draft(_IMPORTED), "catalog": ["teleport"]}, _ctx()
    )
    assert plain.success is True
    assert with_extra.success is True
    assert plain.data["would_block"] == with_extra.data["would_block"] is False


async def test_a_fabricated_tool_blocks_even_when_an_extra_key_claims_it() -> None:
    """No fail-open: a stray extra key can no longer vouch for a tool the org does not have."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED)])
    res = await _connector(repo).execute({**_draft("teleport"), "catalog": ["teleport"]}, _ctx())
    assert res.success is True
    assert res.data["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.data["blocking"])


async def test_a_pending_approval_tool_is_not_available_to_the_gate() -> None:
    """The supply-chain HITL gate is honoured at COMPILE time, where the failure is cheap — an
    unapproved tool would otherwise pass compile and fail later at dispatch (#705's rule)."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED, status="pending_approval")])
    res = await _connector(repo).execute(_draft(_IMPORTED), _ctx())
    assert res.data["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.data["blocking"])


async def test_a_rejected_tool_is_not_available_to_the_gate() -> None:
    repo = _FakeCapabilityRepo([_row(_IMPORTED, status="rejected")])
    res = await _connector(repo).execute(_draft(_IMPORTED), _ctx())
    assert res.data["would_block"] is True


async def test_a_non_tool_descriptor_is_not_available_as_a_tool() -> None:
    """A member's ``tools[]`` names TOOLS — a registered harness row is not one."""
    repo = _FakeCapabilityRepo([_row("some-team", kind=DescriptorKind.HARNESS)])
    res = await _connector(repo).execute(_draft("some-team"), _ctx())
    assert res.data["would_block"] is True


async def test_a_registered_builtin_passes_without_a_repository() -> None:
    # THE FAIL-CLOSED FLOOR: with no repository injected (a unit construction / a degraded start)
    # the gate falls back to the in-process built-ins ONLY. A built-in is genuinely registered, so
    # it still passes — the floor narrows the allowed set, it never widens it.
    res = await _connector(None).execute(_draft(_BUILTIN), _ctx())
    assert res.success is True
    assert res.data["would_block"] is False


async def test_without_a_repository_an_imported_tool_blocks_rather_than_failing_open() -> None:
    res = await _connector(None).execute(_draft(_IMPORTED), _ctx())
    assert res.data["would_block"] is True


async def test_a_repository_failure_degrades_to_the_builtin_floor_never_fails_open() -> None:
    """A registry read that errors must not crash the gate and must not widen it (the same degrade
    policy ``surveyed_catalog`` uses upstream: seed-only on outage, never fail-open)."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED)], explode=True)
    blocked = await _connector(repo).execute(_draft(_IMPORTED), _ctx())
    assert blocked.success is True
    assert blocked.data["would_block"] is True
    clean = await _connector(_FakeCapabilityRepo([], explode=True)).execute(
        _draft(_BUILTIN), _ctx()
    )
    assert clean.data["would_block"] is False


async def test_a_validator_failure_fails_closed_to_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # FAIL CLOSED: if the validator itself raises, the connector returns would_block True — never a
    # verdict the drafter/reviewer could read as "not blocked".
    import oraclous_ohm.compiler as compiler_mod

    def _boom(*_a: object, **_k: object) -> dict:
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(compiler_mod, "validate_draft", _boom)
    ex = _connector(_FakeCapabilityRepo([_row("web-search")]))
    res = await ex.execute(_draft("web-search"), _ctx())
    assert res.success is True
    assert res.data["would_block"] is True


async def test_the_call_succeeds_even_when_the_verdict_is_blocked() -> None:
    """#900's own load-bearing mechanism, called out explicitly in the issue brief: a BLOCKED
    verdict must still be a SUCCESSFUL tool call (``ExecutionResult(success=True, …)``) — the call
    succeeded, the verdict is data — because ADR-053 decision 3's platform-written receipt
    (``driving_signals``) only mints for a successful ``StepKind.TOOL`` step. A blocked draft that
    made ``success=False`` here would silently lose the receipt the run needs, not just the
    verdict. Pinned explicitly even though the sibling connector's own tests imply it, since it is
    load-bearing for THIS issue specifically in a way it is not for ``manifest-validate`` (whose
    caller — the reviewer — never terminates a loop ON this call)."""
    repo = _FakeCapabilityRepo([_row("web-search")])
    res = await _connector(repo).execute(_draft("teleport"), _ctx())
    assert res.success is True
    assert res.data["would_block"] is True


def test_the_tool_carries_no_wrapper_argument() -> None:
    """#900's design decision (see module docstring): the whole call IS the draft, so there is no
    ``"draft"`` key anywhere in the descriptor's declared shape — unlike ``manifest-validate``,
    which requires exactly one."""
    from oraclous_capability_registry_service.domain.plugins.builtin import DraftManifestPlugin

    props = DraftManifestPlugin.INPUT_SCHEMA.get("properties", {})
    assert "draft" not in props
    params = DraftManifestPlugin.CAPABILITIES[0]["parameters"]
    assert "draft" not in params


def test_credential_requirements_are_empty_keyless() -> None:
    from oraclous_capability_registry_service.domain.plugins.builtin import DraftManifestPlugin

    assert DraftManifestPlugin.CREDENTIAL_REQUIREMENTS == []


def test_the_connector_is_registered_with_a_resolving_executor() -> None:
    from oraclous_capability_registry_service.domain.connectors.draft_manifest import (
        DraftManifestConnector,
    )
    from oraclous_capability_registry_service.domain.executors.factory import (
        create_executor,
        has_executor,
    )
    from oraclous_capability_registry_service.domain.plugins.builtin import DraftManifestPlugin

    desc = DraftManifestPlugin.descriptor()
    assert DraftManifestPlugin.NAME == "Draft Manifest"  # slug → draft-manifest
    assert has_executor(desc)
    assert isinstance(create_executor(desc), DraftManifestConnector)


def test_the_plugin_is_discovered_by_the_plugin_registry() -> None:
    from oraclous_capability_registry_service.domain.plugins.base import plugin_registry
    from oraclous_capability_registry_service.domain.plugins.builtin import DraftManifestPlugin

    assert DraftManifestPlugin in plugin_registry.discover()


async def test_sync_plugins_seeds_it_generically_no_migration_no_special_casing() -> None:
    """ "No DB migration needed" pin: ``sync_plugins`` (``services/plugin_sync.py``) already loops
    over ``plugin_registry.discover()`` and upserts EVERY plugin's descriptor generically — it is
    not a hardcoded list of known tool names, so a brand-new ``@plugin_registry.register``-
    decorated class is seeded automatically, through the SAME code path, into the SAME
    ``capability_descriptors`` table (``kind=tool``, ``status`` server-defaults to ``"active"`` —
    no new column, no new enum value). Nothing in ``plugin_sync.py`` itself needs to change for
    ``draft-manifest`` to be seeded; this test proves that by exercising the real, unmodified
    ``sync_plugins`` against a fake repository, the same way the connector tests above prove the
    gate's behaviour without a real database."""
    from oraclous_capability_registry_service.domain.plugins.builtin import DraftManifestPlugin
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    class _FakeUpsertRepo:
        def __init__(self) -> None:
            self.upserted: list[tuple[uuid.UUID, uuid.UUID, DescriptorKind, dict]] = []

        async def upsert_by_id(
            self,
            *,
            organisation_id: uuid.UUID,
            descriptor_id: uuid.UUID,
            kind: DescriptorKind,
            descriptor: dict,
        ) -> tuple[SimpleNamespace, str]:
            self.upserted.append((organisation_id, descriptor_id, kind, descriptor))
            row = SimpleNamespace(
                id=descriptor_id, organisation_id=organisation_id, kind=kind, descriptor=descriptor
            )
            return row, "created"

    repo = _FakeUpsertRepo()
    statuses = await sync_plugins(repository=repo, organisation_id=_ORG)
    assert statuses[DraftManifestPlugin.plugin_id()] == "created"
    matched = [
        d for (_org, _id, kind, d) in repo.upserted if d["metadata"]["name"] == "Draft Manifest"
    ]
    assert matched, "draft-manifest must be seeded through the SAME generic sync path"
    assert matched[0]["kind"] == "tool"
