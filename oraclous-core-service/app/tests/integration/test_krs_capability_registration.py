"""
[tests] KRS capability registration in R2 registry — integration tests

Story: ORAA-62 [R3-CAP-1]
Architecture refs:
  - Section 3 Layer 2:  https://oraclous.atlassian.net/wiki/spaces/OP/pages/65967
  - R2 release page:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - OHM v1.0 Spec:      https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - ADR-010 (TDD):      https://oraclous.atlassian.net/wiki/spaces/OP/pages/557078
  - Test Strategy:      https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

Deps satisfied by:
  - ORAA-56 [R3-KRS-1]  knowledge-retriever-service scaffold (done)
  - ORAA-60 [R3-KRS-3]  NodeResult envelope on all five endpoints (done)

All imports from oraclous_knowledge_retriever_service.capability_registration will
fail with ImportError until the implementer creates that module.

The ImportError on the module-level import below IS the expected initial TDD failure
(ADR-010). Every test in this file is intentionally red until the implementer
delivers the capability-registration module in knowledge-retriever-service.

Behaviours covered:
  CAP-01  capability_registration module is importable from KRS
  CAP-02  RETRIEVER_CAPABILITY_DESCRIPTORS contains exactly 5 entries
  CAP-03  all 5 descriptor entries have kind == "tool"
  CAP-04  all 5 descriptors are OHM-compliant (ToolDescriptor validates without error)
  CAP-05  the 5 descriptor IDs match the canonical retriever endpoint identifiers
  CAP-06  each descriptor carries non-empty input_schema and output_schema
  CAP-07  register_retriever_capabilities() persists all 5 rows in the registry
  CAP-08  list_by_kind(org, TOOL) returns all 5 after provisioning
  CAP-09  content_hash is non-null on every registered row (AC3 — R2 content-hash versioning)
  CAP-10  content_hash is deterministic: same descriptor → same hash on re-registration
  CAP-11  search_by_descriptor(org, {"kind": "tool"}) returns all 5 after provisioning
  CAP-12  org isolation: a different org sees zero rows after KRS provisioning
"""

from __future__ import annotations

import uuid

import pytest

from app.models.capability_descriptor import (
    CapabilityDescriptorDB,
    DescriptorKind,
)
from app.services.capability_registry import CapabilityRegistryService

# Module-level import IS the expected TDD RED signal (ADR-010).
# Fails with ImportError until the implementer creates:
#   services/knowledge-retriever-service/src/oraclous_knowledge_retriever_service/capability_registration.py
from oraclous_knowledge_retriever_service.capability_registration import (
    RETRIEVER_CAPABILITY_DESCRIPTORS,
    register_retriever_capabilities,
)

# ---------------------------------------------------------------------------
# Test org UUIDs
# ---------------------------------------------------------------------------

_ORG_KRS = uuid.UUID("cccccccc-0000-0000-0000-000000000010")  # workspace with retriever
_ORG_OTHER = uuid.UUID("dddddddd-0000-0000-0000-000000000020")  # another workspace, no retriever

# ---------------------------------------------------------------------------
# Canonical retriever endpoint identifiers (AC1 contract)
# ---------------------------------------------------------------------------

_EXPECTED_DESCRIPTOR_IDS = {
    "semantic-search",
    "full-text-search",
    "hybrid-search",
    "graph-traverse",
    "temporal-slice",
}


# ---------------------------------------------------------------------------
# CAP-01  capability_registration module is importable from KRS
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_capability_registration_module_is_importable():
    """oraclous_knowledge_retriever_service.capability_registration must be importable."""
    import oraclous_knowledge_retriever_service.capability_registration as mod

    assert mod is not None


# ---------------------------------------------------------------------------
# CAP-02  RETRIEVER_CAPABILITY_DESCRIPTORS has exactly 5 entries
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_retriever_capability_descriptors_has_five_entries():
    """RETRIEVER_CAPABILITY_DESCRIPTORS must expose exactly five capability dicts — one per endpoint."""
    assert len(RETRIEVER_CAPABILITY_DESCRIPTORS) == 5, (
        f"Expected 5 retriever capability descriptors, got {len(RETRIEVER_CAPABILITY_DESCRIPTORS)}. "
        "Covered endpoints: semantic_search, full_text_search, hybrid_search, graph_traverse, temporal_slice"
    )


# ---------------------------------------------------------------------------
# CAP-03  all 5 descriptor entries have kind == "tool"
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_all_retriever_descriptors_have_kind_tool():
    """Every entry in RETRIEVER_CAPABILITY_DESCRIPTORS must have kind == 'tool' (AC1)."""
    for descriptor in RETRIEVER_CAPABILITY_DESCRIPTORS:
        assert descriptor.get("kind") == "tool", (
            f"Descriptor {descriptor.get('id')!r} has kind={descriptor.get('kind')!r}; "
            "all retriever capabilities must be kind:tool per OHM spec and AC1"
        )


# ---------------------------------------------------------------------------
# CAP-04  all 5 descriptors are OHM-compliant (ToolDescriptor validates)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "descriptor",
    RETRIEVER_CAPABILITY_DESCRIPTORS,
    ids=[d.get("id", f"descriptor-{i}") for i, d in enumerate(RETRIEVER_CAPABILITY_DESCRIPTORS)],
)
def test_retriever_descriptor_is_ohm_compliant(descriptor):
    """Each retriever descriptor must parse as a valid OHM ToolDescriptor without error (AC1)."""
    from pydantic import TypeAdapter

    from ohm.schemas.capability_descriptor import ToolDescriptor

    adapter = TypeAdapter(ToolDescriptor)
    parsed = adapter.validate_python(descriptor)
    assert parsed.kind == "tool"
    assert parsed.id is not None and parsed.id != ""
    assert parsed.metadata.name != ""
    assert parsed.metadata.description != ""
    assert parsed.version.hash != ""
    assert parsed.spec.implementation.type != ""
    assert parsed.spec.implementation.handler != ""


# ---------------------------------------------------------------------------
# CAP-05  the 5 descriptor IDs match the canonical endpoint identifiers
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_descriptor_ids_match_canonical_retriever_endpoints():
    """Descriptor IDs must match the canonical set for the five KRS retriever endpoints."""
    actual_ids = {d.get("id") for d in RETRIEVER_CAPABILITY_DESCRIPTORS}
    assert actual_ids == _EXPECTED_DESCRIPTOR_IDS, (
        f"Descriptor IDs mismatch.\n"
        f"  expected: {sorted(_EXPECTED_DESCRIPTOR_IDS)}\n"
        f"  got:      {sorted(actual_ids)}"
    )


# ---------------------------------------------------------------------------
# CAP-06  each descriptor carries non-empty input_schema and output_schema
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "descriptor",
    RETRIEVER_CAPABILITY_DESCRIPTORS,
    ids=[d.get("id", f"descriptor-{i}") for i, d in enumerate(RETRIEVER_CAPABILITY_DESCRIPTORS)],
)
def test_descriptor_has_input_and_output_schemas(descriptor):
    """Each descriptor spec must define both input_schema and output_schema (OHM ToolSpec contract)."""
    spec = descriptor.get("spec", {})
    input_schema = spec.get("input_schema")
    output_schema = spec.get("output_schema")
    assert input_schema, (
        f"Descriptor {descriptor.get('id')!r} is missing input_schema in spec"
    )
    assert output_schema, (
        f"Descriptor {descriptor.get('id')!r} is missing output_schema in spec"
    )
    assert isinstance(input_schema, dict), (
        f"Descriptor {descriptor.get('id')!r} input_schema must be a dict"
    )
    assert isinstance(output_schema, dict), (
        f"Descriptor {descriptor.get('id')!r} output_schema must be a dict"
    )


# ---------------------------------------------------------------------------
# CAP-07  register_retriever_capabilities() persists all 5 rows in the registry
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_register_retriever_capabilities_persists_five_rows(async_session):
    """register_retriever_capabilities() must create exactly five CapabilityDescriptorDB rows.

    This is the primary integration path for AC1: all 5 appear in the registry as
    kind:tool after a single provisioning call.
    """
    svc = CapabilityRegistryService(async_session)
    rows = await register_retriever_capabilities(svc, org_id=_ORG_KRS)

    assert len(rows) == 5, (
        f"register_retriever_capabilities() must return 5 rows; got {len(rows)}"
    )
    for row in rows:
        assert isinstance(row, CapabilityDescriptorDB)
        assert row.id is not None
        assert row.org_id == _ORG_KRS
        assert row.kind == DescriptorKind.TOOL


# ---------------------------------------------------------------------------
# CAP-08  list_by_kind(org, TOOL) returns all 5 after provisioning (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_by_kind_returns_all_five_retriever_tools(async_session):
    """After provisioning, list_by_kind(TOOL) for the KRS org must return exactly 5 rows (AC2).

    Simulates the integration test criterion: registry returns all 5 for workspace
    with retriever provisioned.
    """
    svc = CapabilityRegistryService(async_session)
    await register_retriever_capabilities(svc, org_id=_ORG_KRS)

    tools = await svc.list_by_kind(_ORG_KRS, DescriptorKind.TOOL)
    assert len(tools) == 5, (
        f"Expected 5 tool capabilities after KRS provisioning; got {len(tools)}"
    )
    registered_ids = {row.descriptor["id"] for row in tools}
    assert registered_ids == _EXPECTED_DESCRIPTOR_IDS, (
        f"Registry tool IDs mismatch after provisioning.\n"
        f"  expected: {sorted(_EXPECTED_DESCRIPTOR_IDS)}\n"
        f"  got:      {sorted(registered_ids)}"
    )


# ---------------------------------------------------------------------------
# CAP-09  content_hash is non-null on every registered row (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.capability_integrity
async def test_all_registered_rows_have_content_hash(async_session):
    """Every registered row must carry a non-null content_hash (AC3 — R2 content-hash versioning).

    The CapabilityRegistryService/repo auto-computes the hash from the descriptor
    body on create(). This test verifies that the auto-compute path is exercised
    for all five retriever capabilities.
    """
    svc = CapabilityRegistryService(async_session)
    rows = await register_retriever_capabilities(svc, org_id=_ORG_KRS)

    for row in rows:
        assert row.content_hash is not None, (
            f"Row for descriptor {row.descriptor.get('id')!r} has null content_hash; "
            "R2 content-hash versioning requires every descriptor row to carry a computed hash"
        )
        assert len(row.content_hash) == 64, (
            f"content_hash for {row.descriptor.get('id')!r} is not a 64-char SHA-256 hex digest; "
            f"got length {len(row.content_hash)}"
        )


# ---------------------------------------------------------------------------
# CAP-10  content_hash is deterministic: same descriptor → same hash on re-registration
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.capability_integrity
async def test_content_hash_is_deterministic(async_session):
    """Registering the same descriptor twice must yield the same content_hash (R2 stability).

    Verifies that compute_content_hash is canonical (sorted keys, no None noise).
    """
    svc = CapabilityRegistryService(async_session)

    first_rows = await register_retriever_capabilities(svc, org_id=_ORG_KRS)
    second_rows = await register_retriever_capabilities(svc, org_id=_ORG_KRS)

    first_hashes = {row.descriptor["id"]: row.content_hash for row in first_rows}
    second_hashes = {row.descriptor["id"]: row.content_hash for row in second_rows}

    for descriptor_id in _EXPECTED_DESCRIPTOR_IDS:
        assert first_hashes[descriptor_id] == second_hashes[descriptor_id], (
            f"content_hash for {descriptor_id!r} is non-deterministic: "
            f"{first_hashes[descriptor_id]!r} != {second_hashes[descriptor_id]!r}"
        )


# ---------------------------------------------------------------------------
# CAP-11  search_by_descriptor returns all 5 after provisioning
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_by_kind_tool_returns_all_retriever_capabilities(async_session):
    """search_by_descriptor(org, {"kind": "tool"}) must return all 5 after provisioning."""
    svc = CapabilityRegistryService(async_session)
    await register_retriever_capabilities(svc, org_id=_ORG_KRS)

    results = await svc.search_by_descriptor(_ORG_KRS, {"kind": "tool"})
    assert len(results) == 5, (
        f"search_by_descriptor with kind:tool returned {len(results)} rows; expected 5"
    )
    result_ids = {row.descriptor["id"] for row in results}
    assert result_ids == _EXPECTED_DESCRIPTOR_IDS


# ---------------------------------------------------------------------------
# CAP-12  org isolation: another org sees zero rows after KRS provisioning
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.organization_isolation
async def test_krs_capabilities_not_visible_to_other_org(async_session):
    """Capabilities provisioned for the KRS org must not be visible to another org.

    Enforces the fundamental org_id tenancy boundary on capability_descriptor.
    """
    svc = CapabilityRegistryService(async_session)
    await register_retriever_capabilities(svc, org_id=_ORG_KRS)

    other_org_tools = await svc.list_by_kind(_ORG_OTHER, DescriptorKind.TOOL)
    assert other_org_tools == [], (
        f"Org {_ORG_OTHER} must not see capabilities provisioned for {_ORG_KRS}; "
        f"got {len(other_org_tools)} rows — org isolation breach"
    )
