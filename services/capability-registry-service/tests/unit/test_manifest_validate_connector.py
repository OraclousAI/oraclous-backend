"""Unit: the ManifestValidateConnector (#594 / ADR-047) — wraps ohm ``validate_draft`` as a tool.

The decisive checks: a clean drafted team passes (would_block False); a hallucinated tool the org
never registered BLOCKS with F-CAPABILITY-MISSING — the deterministic capability-absence gate
(ADR-032), even when the draft arrives as the LLM's ```json-fenced TEXT (the connector peels it); a
missing draft never runs; and the connector is registered as a builtin (slug ``manifest-validate``)
with an executor whose synthesized ``core/manifest-validate@1`` ref resolves.

#705 — THE GATE READS THE REGISTRY, NEVER A MODEL'S COPY. The allowed set is sourced from the
calling org's registered TOOL descriptors, by code, on every call. The relayed ``catalog`` argument
is gone: it was typed into the tool call by the reviewer (an LLM), and for an imported MCP tool
(a descriptor row, not an in-process plugin) that relay was the ONLY source — one dropped name
falsely blocked a whole compile. A deterministic validator fed a model-authored fact is not
deterministic (ADR-043). The degrade is fail-CLOSED: with no repository (a unit construction) or a
repository that errors, the gate falls back to the in-process built-ins ONLY — it never widens.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.connectors.manifest_validate import (
    ManifestValidateConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import (
    create_executor,
    has_executor,
)
from oraclous_capability_registry_service.domain.plugins.builtin import ManifestValidatePlugin
from oraclous_capability_registry_service.models.enums import DescriptorKind

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")
#: an imported, approved MCP tool — a descriptor ROW, so the in-process plugin registry cannot
#: see it. This is exactly the tool the deployed compile falsely blocked (#705).
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
    return {
        "members": [
            {"role": "researcher", "kind": "agent", "manifest_ref": "org:x/r@1", "tools": [tool]},
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
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
    """Stands in for ``CapabilityRepository`` — the org's registered descriptor rows."""

    def __init__(self, rows: list[SimpleNamespace], *, explode: bool = False) -> None:
        self._rows = rows
        self._explode = explode
        #: every org id the connector scoped a read to (the gate must use the CALLER's org)
        self.orgs: list[uuid.UUID] = []

    async def list_by_kind(
        self, organisation_id: uuid.UUID, kind: DescriptorKind
    ) -> list[SimpleNamespace]:
        self.orgs.append(organisation_id)
        if self._explode:
            raise RuntimeError("registry read failed")
        return [r for r in self._rows if r.kind == kind]


def _connector(repo: _FakeCapabilityRepo | None) -> ManifestValidateConnector:
    ex = ManifestValidateConnector({"id": "x"})
    if repo is not None:
        ex.capability_repo = repo  # injected by ToolExecutionService on the live path
    return ex


async def test_a_clean_draft_passes() -> None:
    # the org has the tool registered → the drafted team is ready to run
    ex = _connector(_FakeCapabilityRepo([_row("web-search")]))
    res = await ex.execute({"draft": _draft("web-search")}, _ctx())
    assert res.success is True
    assert res.data["would_block"] is False


async def test_a_hallucinated_tool_blocks_fail_closed() -> None:
    ex = _connector(_FakeCapabilityRepo([_row("web-search")]))
    # the draft arrives as the reviewer-relayed LLM TEXT (a ```json fence) — the connector peels it
    draft_text = "Here is the team:\n```json\n" + json.dumps(_draft("teleport")) + "\n```"
    res = await ex.execute({"draft": draft_text}, _ctx())
    assert res.success is True  # the validation RAN (would_block is data, not a tool failure)
    assert res.data["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.data["blocking"])


async def test_an_imported_mcp_tool_passes_with_no_relayed_catalog() -> None:
    """#705 regression — the defect that blocked run 5105ed55.

    An imported, approved MCP tool is a descriptor row the in-process plugin registry cannot see.
    With the relay gone the gate must still admit it, sourced from the org's registry.
    """
    repo = _FakeCapabilityRepo([_row(_IMPORTED)])
    res = await _connector(repo).execute({"draft": _draft(_IMPORTED)}, _ctx())
    assert res.success is True
    assert res.data["would_block"] is False
    assert repo.orgs == [_ORG]  # org-scoped: the gate reads the CALLER's registry (ADR-006)


async def test_the_verdict_ignores_the_relayed_catalog_entirely() -> None:
    """The gate's verdict is identical for a full relay, a partial relay and no relay at all."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED), _row("web-search")])
    relays: list[Any] = [
        {},  # no relay at all
        {"catalog": []},  # an empty relay
        {"catalog": ["web-search"]},  # a partial relay that DROPPED the imported tool
        {"catalog": [_IMPORTED, "web-search"]},  # a full relay
        {"catalog": {"tools": [{"name": "web-search"}]}},  # the surveyor's dict shape, partial
    ]
    verdicts = []
    for relay in relays:
        res = await _connector(repo).execute({"draft": _draft(_IMPORTED), **relay}, _ctx())
        assert res.success is True
        verdicts.append(res.data["would_block"])
    assert verdicts == [False] * len(relays)


async def test_a_fabricated_tool_blocks_even_when_the_relay_claims_it() -> None:
    """No fail-open: a relayed catalog can no longer vouch for a tool the org does not have."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED)])
    res = await _connector(repo).execute(
        {"draft": _draft("teleport"), "catalog": ["teleport"]}, _ctx()
    )
    assert res.success is True
    assert res.data["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.data["blocking"])


async def test_a_pending_approval_tool_is_not_available_to_the_gate() -> None:
    """The supply-chain HITL gate is honoured at COMPILE time, where the failure is cheap — an
    unapproved tool would otherwise pass compile and fail later at dispatch (#705, one decision)."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED, status="pending_approval")])
    res = await _connector(repo).execute({"draft": _draft(_IMPORTED)}, _ctx())
    assert res.data["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.data["blocking"])


async def test_a_rejected_tool_is_not_available_to_the_gate() -> None:
    repo = _FakeCapabilityRepo([_row(_IMPORTED, status="rejected")])
    res = await _connector(repo).execute({"draft": _draft(_IMPORTED)}, _ctx())
    assert res.data["would_block"] is True


async def test_a_non_tool_descriptor_is_not_available_as_a_tool() -> None:
    """A member's ``tools[]`` names TOOLS — a registered harness row is not one."""
    repo = _FakeCapabilityRepo([_row("some-team", kind=DescriptorKind.HARNESS)])
    res = await _connector(repo).execute({"draft": _draft("some-team")}, _ctx())
    assert res.data["would_block"] is True


async def test_a_registered_builtin_passes_without_a_repository() -> None:
    # THE FAIL-CLOSED FLOOR: with no repository injected (a unit construction / a degraded start)
    # the gate falls back to the in-process built-ins ONLY. A built-in is genuinely registered, so
    # it still passes — the floor narrows the allowed set, it never widens it.
    res = await _connector(None).execute({"draft": _draft(_BUILTIN)}, _ctx())
    assert res.success is True
    assert res.data["would_block"] is False


async def test_without_a_repository_an_imported_tool_blocks_rather_than_failing_open() -> None:
    res = await _connector(None).execute(
        {"draft": _draft(_IMPORTED), "catalog": [_IMPORTED]}, _ctx()
    )
    assert res.data["would_block"] is True


async def test_a_repository_failure_degrades_to_the_builtin_floor_never_fails_open() -> None:
    """A registry read that errors must not crash the gate and must not widen it (the same degrade
    policy ``surveyed_catalog`` uses upstream: seed-only on outage, never fail-open)."""
    repo = _FakeCapabilityRepo([_row(_IMPORTED)], explode=True)
    blocked = await _connector(repo).execute({"draft": _draft(_IMPORTED)}, _ctx())
    assert blocked.success is True
    assert blocked.data["would_block"] is True
    clean = await _connector(_FakeCapabilityRepo([], explode=True)).execute(
        {"draft": _draft(_BUILTIN)}, _ctx()
    )
    assert clean.data["would_block"] is False


async def test_a_missing_draft_is_rejected_before_validating() -> None:
    res = await _connector(_FakeCapabilityRepo([_row("web-search")])).execute({}, _ctx())
    assert res.success is False
    assert res.error_type == "INVALID_INPUT"


async def test_a_validator_failure_fails_closed_to_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # FAIL CLOSED: if the validator itself raises, the connector returns would_block True — never a
    # verdict the reviewer (an LLM) could read as "not blocked" and emit an unvalidated team.
    import oraclous_ohm.compiler as compiler_mod

    def _boom(*_a: object, **_k: object) -> dict:
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(compiler_mod, "validate_draft", _boom)
    ex = _connector(_FakeCapabilityRepo([_row("web-search")]))
    res = await ex.execute({"draft": _draft("web-search")}, _ctx())
    assert res.success is True
    assert res.data["would_block"] is True


def test_the_tool_no_longer_advertises_a_relayed_catalog_argument() -> None:
    """#705 item 2 — a dead parameter invites the same bug back. The gate sources the catalog
    itself, so there is nothing for a caller to relay."""
    props = ManifestValidatePlugin.INPUT_SCHEMA["properties"]
    assert "draft" in props
    assert "catalog" not in props
    params = ManifestValidatePlugin.CAPABILITIES[0]["parameters"]
    assert "catalog" not in params


def test_the_connector_is_registered_with_a_resolving_executor() -> None:
    desc = ManifestValidatePlugin.descriptor()
    assert ManifestValidatePlugin.NAME == "Manifest Validate"  # slug → manifest-validate
    assert has_executor(desc)
    assert isinstance(create_executor(desc), ManifestValidateConnector)
