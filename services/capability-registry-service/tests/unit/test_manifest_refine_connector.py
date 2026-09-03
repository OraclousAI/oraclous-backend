"""Unit: the ManifestRefineConnector (#595 / ADR-047 §4) — wraps ohm ``apply_refine`` as a tool.

#708 — THE ALLOWED SET IS READ, NOT RELAYED (the same fix #705 already gave ``manifest-validate``).
The connector used to read ``input_data.get("catalog")`` — a caller-supplied relay — and union it
with only the in-process plugin registry, so an imported MCP tool (a ``capability_repository`` ROW,
not an in-process plugin) could never be added to an EXISTING team via refine: an approved, active,
imported tool always blocked. The shared helper (``_catalog.py::read_allowed_catalog``, factored out
of ``ManifestValidateConnector._allowed_catalog``) now backs both connectors; the relay is gone
entirely — the connector reads the org's registered TOOL descriptors itself, by code.

A clean op applies (preserve-the-rest, would_block False); an unsurveyed tool / bad op / cyclic
delta blocks (would_block True, applied False, manifest None) — never a silent apply; a missing
manifest/op is rejected; the connector is registered (slug ``manifest-refine``) with an executor
whose synthesized ``core/manifest-refine@1`` ref resolves.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from oraclous_capability_registry_service.domain.connectors.manifest_refine import (
    ManifestRefineConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import (
    create_executor,
    has_executor,
)
from oraclous_capability_registry_service.domain.plugins.builtin import ManifestRefinePlugin
from oraclous_capability_registry_service.models.enums import DescriptorKind

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
#: an imported, approved MCP tool — a descriptor ROW, so the in-process plugin registry cannot see
#: it. The same fixture ``manifest-validate``'s own #705 tests use for the identical defect.
_IMPORTED = "github-mcp-add-issue-comment"
#: a genuinely registered built-in (a plugin class compiled into the service) — admissible with or
#: without a repository, since the in-process plugin registry always carries it.
_BUILTIN = "web-research"


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
    )


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
    """Stands in for ``CapabilityRepository`` — the org's registered descriptor rows. Mirrors
    ``test_manifest_validate_connector.py``'s fixture exactly (both org-scoped read methods offered,
    so these tests pin BEHAVIOUR and leave the query-vs-filter split to the implementation)."""

    def __init__(self, rows: list[SimpleNamespace], *, explode: bool = False) -> None:
        self._rows = rows
        self._explode = explode
        #: every org id the connector scoped a read to (the gate must use the CALLER's org)
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


def _connector(repo: _FakeCapabilityRepo | None) -> ManifestRefineConnector:
    ex = ManifestRefineConnector({"id": "x"})
    if repo is not None:
        ex.capability_repo = repo  # injected by ToolExecutionService on the live path
    return ex


def _manifest(researcher_tool: str = _BUILTIN) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "t",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/r@1",
                "tools": [researcher_tool],
            },
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
            },
        ],
        "runtime": {"entrypoint": "researcher"},
    }


def _add_member_op(tool: str) -> dict:
    return {
        "op": "add_member",
        "role": "fact-checker",
        "tools": [tool],
        "depends_on": ["researcher"],
    }


async def test_a_clean_refine_applies_with_no_relayed_catalog_at_all() -> None:
    # #708: proves the relay is gone — no "catalog" key in the input whatsoever, and the added
    # tool still applies because it is a genuinely registered built-in.
    ex = _connector(_FakeCapabilityRepo([]))
    res = await ex.execute({"manifest": _manifest(), "edit_op": _add_member_op(_BUILTIN)}, _ctx())
    assert res.success is True
    assert res.data["applied"] is True and res.data["would_block"] is False
    roles = {m["role"] for m in res.data["manifest"]["members"]}
    assert "fact-checker" in roles and {"researcher", "writer"} <= roles


async def test_an_imported_mcp_tool_applies_with_no_relayed_catalog() -> None:
    """#708 — the direct acceptance criterion. An imported, approved MCP tool is a descriptor row
    the in-process plugin registry cannot see; the refine gate must still admit it, sourced from
    the org's registry, exactly like ``manifest-validate`` (#705)."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED)])
    res = await _connector(repo).execute(
        {"manifest": _manifest(_IMPORTED), "edit_op": _add_member_op(_IMPORTED)}, _ctx()
    )
    assert res.success is True
    assert res.data["would_block"] is False and res.data["applied"] is True
    assert repo.orgs == [_ORG]  # org-scoped: the gate reads the CALLER's registry (ADR-006)


async def test_a_fabricated_tool_blocks_even_when_the_relay_claims_it() -> None:
    """No fail-open: even when a caller still sends a "catalog" key claiming a hallucinated tool,
    it is IGNORED entirely — the tool still blocks. Uses ``_manifest()``'s default (a genuine
    built-in, admissible with or without the fix) for the pre-existing member so the ONLY possible
    source of a block is the fabricated tool on the newly-added member, not a relay coincidentally
    under-declaring an existing one."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED)])
    res = await _connector(repo).execute(
        {
            "manifest": _manifest(),
            "edit_op": {"op": "add_member", "role": "rogue", "tools": ["delete-everything"]},
            "catalog": ["delete-everything"],  # a relay claiming the hallucinated tool — ignored
        },
        _ctx(),
    )
    assert res.success is True
    assert res.data["would_block"] is True and res.data["applied"] is False
    assert res.data["manifest"] is None


async def test_a_pending_approval_tool_is_not_available_to_the_gate() -> None:
    """The supply-chain HITL gate is honoured at refine time too — matching the compile gate's
    #705 decision: an unapproved tool never becomes addable via refine either, even when a relay
    claims it (a relay the fix ignores entirely — without that, this scenario would pass on old
    code, since the relay alone would admit it)."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED, status="pending_approval")])
    res = await _connector(repo).execute(
        {
            "manifest": _manifest(),
            "edit_op": _add_member_op(_IMPORTED),
            "catalog": [_IMPORTED],
        },
        _ctx(),
    )
    assert res.data["would_block"] is True and res.data["applied"] is False


async def test_without_a_repository_the_gate_degrades_to_the_builtin_floor() -> None:
    # THE FAIL-CLOSED FLOOR: with no repository injected (a unit construction) the gate falls back
    # to the in-process built-ins ONLY — it never widens.
    res = await _connector(None).execute(
        {"manifest": _manifest(), "edit_op": _add_member_op(_BUILTIN)}, _ctx()
    )
    assert res.success is True
    assert res.data["would_block"] is False  # a genuine built-in still passes


async def test_without_a_repository_an_imported_tool_blocks_rather_than_failing_open() -> None:
    res = await _connector(None).execute(
        {
            "manifest": _manifest(),
            "edit_op": _add_member_op(_IMPORTED),
            "catalog": [_IMPORTED],  # a relay still cannot vouch for it
        },
        _ctx(),
    )
    assert res.data["would_block"] is True


async def test_a_malformed_op_fails_closed() -> None:
    ex = ManifestRefineConnector({"id": "x"})
    res = await ex.execute({"manifest": _manifest(), "edit_op": {"op": "nonsense"}}, _ctx())
    assert res.success is True and res.data["would_block"] is True and res.data["applied"] is False


async def test_a_missing_manifest_or_op_is_rejected() -> None:
    ex = ManifestRefineConnector({"id": "x"})
    no_manifest = await ex.execute({"edit_op": {"op": "add_member", "role": "x"}}, _ctx())
    assert no_manifest.success is False and no_manifest.error_type == "INVALID_INPUT"
    no_op = await ex.execute({"manifest": _manifest()}, _ctx())
    assert no_op.success is False and no_op.error_type == "INVALID_INPUT"


def test_the_tool_no_longer_advertises_a_relayed_catalog_argument() -> None:
    """#708 — mirrors ``manifest-validate``'s own #705 test: a dead parameter invites the relay
    bug back."""
    props = ManifestRefinePlugin.INPUT_SCHEMA["properties"]
    assert "manifest" in props and "edit_op" in props
    assert "catalog" not in props
    params = ManifestRefinePlugin.CAPABILITIES[0]["parameters"]
    assert "catalog" not in params


def test_the_connector_is_registered_with_a_resolving_executor() -> None:
    desc = ManifestRefinePlugin.descriptor()
    assert ManifestRefinePlugin.NAME == "Manifest Refine"  # slug → manifest-refine
    assert has_executor(desc)
    assert isinstance(create_executor(desc), ManifestRefineConnector)
