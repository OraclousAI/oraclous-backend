"""Unit: the harness binds the team's precedence onto each capability instance (#538 _materialise).

The team-run threads ``precedence_order`` + ``graph_authoritative`` to ``HarnessExecutionService``;
``_materialise`` stamps them onto every instance's config — like the #524 ``graph_id`` bind — so
the knowledge-retriever connector reads ``configuration["precedence"]`` and ranks a member's
in-loop read canonical-first (#536). No precedence → the config is unchanged (back-compat).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_DESCRIPTOR = {"id": "cap-1", "metadata": {"name": "Retriever"}, "spec": {"capabilities": []}}


class _RecordingRegistry:
    """Captures the configuration each created instance receives."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def list_instances(self) -> list[dict]:
        return []

    async def create_instance(self, *, capability_id: str, name: str, configuration: dict) -> dict:
        self.created.append(configuration)
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(self, instance_id: uuid.UUID, mappings: dict) -> dict:
        return {}


def _service(registry: _RecordingRegistry) -> HarnessExecutionService:
    return HarnessExecutionService(
        registry=registry,
        broker=None,
        executions=None,
        assignments=None,
        checkpoints=None,
        provenance=None,
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


def _manifest() -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref="core/knowledge-retriever@1.0.0", binding="retriever")],
        runtime=OHMRuntime(entrypoint="retriever"),
    )


_RESOLVED = {"retriever": {"id": "cap-1", "descriptor": _DESCRIPTOR}}


async def test_precedence_is_bound_onto_the_instance_config() -> None:
    registry = _RecordingRegistry()
    await _service(registry)._materialise(
        _manifest(),
        _RESOLVED,
        precedence_order=["rules", "bible", "drafts"],
        graph_authoritative=True,
    )
    assert len(registry.created) == 1
    assert registry.created[0]["precedence"] == {
        "order": ["rules", "bible", "drafts"],
        "graph_authoritative": True,
    }


async def test_no_precedence_leaves_the_instance_config_unbound() -> None:
    registry = _RecordingRegistry()
    await _service(registry)._materialise(_manifest(), _RESOLVED)
    assert "precedence" not in registry.created[0]  # additive — absent precedence = unchanged


async def test_derived_mode_binds_graph_authoritative_false() -> None:
    """The default/derived mode (graph_authoritative omitted → False) is the common case — pin that
    the False value flows through faithfully (not hardcoded True / coerced)."""
    registry = _RecordingRegistry()
    await _service(registry)._materialise(
        _manifest(), _RESOLVED, precedence_order=["rules", "bible"]
    )
    assert registry.created[0]["precedence"] == {
        "order": ["rules", "bible"],
        "graph_authoritative": False,
    }
