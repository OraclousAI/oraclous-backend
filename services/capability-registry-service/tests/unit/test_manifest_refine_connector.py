"""Unit: the ManifestRefineConnector (#595 / ADR-047 §4) — wraps ohm ``apply_refine`` as a tool.

A clean op applies (preserve-the-rest, would_block False); an unsurveyed tool / bad op / cyclic
delta blocks (would_block True, applied False, manifest None) — never a silent apply; a missing
manifest/op is rejected; the connector is registered (slug ``manifest-refine``) with an executor
whose synthesized ``core/manifest-refine@1`` ref resolves.
"""

from __future__ import annotations

import uuid

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

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
    )


def _manifest() -> dict:
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
                "tools": ["web-research"],
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


async def test_a_clean_refine_applies_and_preserves_the_rest() -> None:
    ex = ManifestRefineConnector({"id": "x"})
    res = await ex.execute(
        {
            "manifest": _manifest(),
            "edit_op": {
                "op": "add_member",
                "role": "fact-checker",
                "tools": ["web-research"],
                "depends_on": ["researcher"],
            },
            "catalog": ["web-research"],
        },
        _ctx(),
    )
    assert res.success is True
    assert res.data["applied"] is True and res.data["would_block"] is False
    roles = {m["role"] for m in res.data["manifest"]["members"]}
    assert "fact-checker" in roles and {"researcher", "writer"} <= roles


async def test_an_unsurveyed_tool_blocks_and_does_not_apply() -> None:
    ex = ManifestRefineConnector({"id": "x"})
    res = await ex.execute(
        {
            "manifest": _manifest(),
            "edit_op": {"op": "add_member", "role": "rogue", "tools": ["delete-everything"]},
            "catalog": ["web-research"],
        },
        _ctx(),
    )
    assert res.success is True  # the validation ran
    assert res.data["would_block"] is True and res.data["applied"] is False
    assert res.data["manifest"] is None


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


def test_the_connector_is_registered_with_a_resolving_executor() -> None:
    desc = ManifestRefinePlugin.descriptor()
    assert ManifestRefinePlugin.NAME == "Manifest Refine"  # slug → manifest-refine
    assert has_executor(desc)
    assert isinstance(create_executor(desc), ManifestRefineConnector)
