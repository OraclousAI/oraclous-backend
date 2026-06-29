"""Unit: the ManifestValidateConnector (#594 / ADR-047) — wraps ohm ``validate_draft`` as a tool.

The decisive checks: a clean drafted team passes (would_block False); a hallucinated tool the
catalog never surveyed BLOCKS with F-CAPABILITY-MISSING — the deterministic capability-absence gate
(ADR-032), even when the draft arrives as the LLM's ```json-fenced TEXT (the connector peels it); a
missing draft never runs; and the connector is registered as a builtin (slug ``manifest-validate``)
with an executor whose synthesized ``core/manifest-validate@1`` ref resolves.
"""

from __future__ import annotations

import json
import uuid

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

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005a1")


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


async def test_a_clean_draft_passes() -> None:
    ex = ManifestValidateConnector({"id": "x"})
    res = await ex.execute({"draft": _draft("web-search"), "catalog": ["web-search"]}, _ctx())
    assert res.success is True
    assert res.data["would_block"] is False  # every tool surveyed → ready


async def test_a_hallucinated_tool_blocks_fail_closed() -> None:
    ex = ManifestValidateConnector({"id": "x"})
    # the draft arrives as the reviewer-relayed LLM TEXT (a ```json fence) — the connector peels it
    draft_text = "Here is the team:\n```json\n" + json.dumps(_draft("teleport")) + "\n```"
    res = await ex.execute({"draft": draft_text, "catalog": ["web-search"]}, _ctx())
    assert res.success is True  # the validation RAN (would_block is data, not a tool failure)
    assert res.data["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.data["blocking"])


async def test_a_registered_builtin_passes_without_a_relayed_catalog() -> None:
    # THE DETERMINISTIC REGISTRY-DIFF: even when the reviewer relays NO catalog, a tool that IS a
    # registered built-in (web-research) is allowed — the gate diffs against the live registry, not
    # the model's relay (the deployed run showed the LLM does not reliably relay the catalog).
    ex = ManifestValidateConnector({"id": "x"})
    res = await ex.execute({"draft": _draft("web-research")}, _ctx())
    assert res.success is True
    assert res.data["would_block"] is False


async def test_a_missing_draft_is_rejected_before_validating() -> None:
    ex = ManifestValidateConnector({"id": "x"})
    res = await ex.execute({"catalog": ["web-search"]}, _ctx())
    assert res.success is False
    assert res.error_type == "INVALID_INPUT"


def test_the_connector_is_registered_with_a_resolving_executor() -> None:
    desc = ManifestValidatePlugin.descriptor()
    assert ManifestValidatePlugin.NAME == "Manifest Validate"  # slug → manifest-validate
    assert has_executor(desc)
    assert isinstance(create_executor(desc), ManifestValidateConnector)
