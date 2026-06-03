"""
[tests] harness capability allocation — integration + security — ORAA-77

Story: ORAA-77 / ORA-76
Architecture refs:
  - R2 release page (T2-M3):    https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - OHM v1.0 Spec:              https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Test Strategy:              https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940
  - ADR-010 (TDD):              https://oraclous.atlassian.net/wiki/spaces/OP/pages/557078

All imports from app.services.capability_allocation are function-local per ADR-010 /
CLAUDE.md §4.1 — pytest collection succeeds and each test fails at runtime (RED-by-design).

Security-marked tests confirm T2-M3 (privilege escalation) at the data layer.  They must
NEVER be converted to skips (CLAUDE.md §4.1 — a skip hides an unverified threat).

Behaviours covered:
  I01  allocate() returns one InvocationHandle per in-scope capability
  I02  allocate() raises ScopeViolationError when capability oauth scope ⊄ harness scope
  I03  allocate() succeeds for capability with no credential_requirements, any harness scope
  I04  allocate() for empty harness scope + credential-free capability returns handle
  I05  allocate() with multiple in-scope capabilities returns one handle per capability
  I06  allocate() is atomic: any violation rejects the entire batch
  I07  InvocationHandle.capability_id matches the allocated CapabilityDescriptorDB.id
  I08  InvocationHandle.kind matches the allocated CapabilityDescriptorDB.kind
  I09  InvocationHandle.descriptor matches the full CapabilityDescriptorDB.descriptor JSONB
  I10  Allocation persisted: list_allocations_for_harness() returns allocated capability IDs
  I11  list_allocations_for_harness() returns [] for a harness with no allocations

  T2-M3 security tests (markers: integration + security):
  S01  [T2-M3] narrow-scope harness cannot allocate a write-credential capability
  S02  [T2-M3] zero-scope harness cannot allocate any oauth-credential capability
  S03  [T2-M3] ScopeViolationError.violating_scopes names the specific blocked scope(s)
  S04  [T2-M3] single out-of-scope capability rejects the entire batch (no partial grants)

  Organization isolation (markers: integration + security + organization_isolation):
  O01  org_B harness cannot read org_A capability allocations
  O02  org_A can allocate and read its own capabilities (positive case)
"""

from __future__ import annotations

import uuid

import pytest

from app.models.capability_descriptor import DescriptorKind

# ---------------------------------------------------------------------------
# Shared test UUIDs
# ---------------------------------------------------------------------------

_ORG_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_ORG_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")

# ---------------------------------------------------------------------------
# Minimal OHM descriptor builders
# ---------------------------------------------------------------------------


def _harness_descriptor(
    harness_id: str,
    credential_scope: list[str],
    name: str = "Test Harness",
) -> dict:
    """Build a minimal valid OHM harness descriptor with a declared credential_scope.

    The spec.credential_scope field carries the set of OAuth scopes this harness
    is authorised to use (T2-M3 enforcement surface).
    """
    return {
        "kind": "harness",
        "id": harness_id,
        "version": {"hash": f"sha256:{harness_id[:8]}", "tags": []},
        "metadata": {"name": name, "description": f"Harness {harness_id}."},
        "spec": {
            "goal": f"Test goal for {harness_id}",
            "actors": [],
            "orchestration": None,
            "credential_scope": credential_scope,
        },
    }


def _tool_descriptor(
    tool_id: str,
    credential_requirements: list[dict],
    name: str = "Test Tool",
) -> dict:
    """Build a minimal valid OHM tool descriptor with the given credential_requirements."""
    return {
        "kind": "tool",
        "id": tool_id,
        "version": {"hash": f"sha256:{tool_id[:8]}", "tags": []},
        "metadata": {"name": name, "description": f"Tool {tool_id}."},
        "spec": {
            "implementation": {"type": "internal", "handler": f"tests.{tool_id}"},
            "input_schema": {"type": "object", "properties": {}},
            "output_schema": {"type": "object", "properties": {}},
            "credential_requirements": credential_requirements,
        },
    }


# ---------------------------------------------------------------------------
# I01  allocate() returns one InvocationHandle per in-scope capability
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_allocate_returns_invocation_handle_for_valid_scope(async_session) -> None:
    """allocate() returns one InvocationHandle per capability when harness scope covers all.

    Harness declares ["drive:read"]; tool requires ["drive:read"]. Allocation succeeds.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, InvocationHandle
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i01", credential_scope=["drive:read"]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-i01",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]}
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_row.id],
    )

    assert len(handles) == 1
    assert isinstance(handles[0], InvocationHandle)


# ---------------------------------------------------------------------------
# I02  allocate() raises ScopeViolationError when scope not covered
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_allocate_raises_scope_violation_error_when_scope_exceeds_harness(
    async_session,
) -> None:
    """allocate() raises ScopeViolationError when capability requires a scope not in harness.

    T2-M3: a harness declaring only "drive:read" must not be permitted to allocate
    a tool that requires "drive:write".
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, ScopeViolationError
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i02", credential_scope=["drive:read"]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-i02",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:write"]}
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    with pytest.raises(ScopeViolationError):
        await allocator.allocate(
            org_id=_ORG_A,
            harness_id=harness_row.id,
            capability_ids=[tool_row.id],
        )


# ---------------------------------------------------------------------------
# I03  allocate() succeeds for capability with no credential_requirements
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_allocate_credential_free_capability_always_succeeds(async_session) -> None:
    """A capability with no credential_requirements can be allocated regardless of harness scope.

    Credential-free tools (e.g. text formatters) are safe to allocate to any harness.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, InvocationHandle
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i03", credential_scope=[]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-i03", credential_requirements=[]),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_row.id],
    )

    assert len(handles) == 1
    assert isinstance(handles[0], InvocationHandle)


# ---------------------------------------------------------------------------
# I04  allocate() for empty harness scope + credential-free capability returns handle
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_allocate_empty_scope_harness_with_no_cred_tool_succeeds(async_session) -> None:
    """A harness with zero declared scope can still allocate credential-free capabilities."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i04", credential_scope=[]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-i04-nocred", credential_requirements=[]),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_row.id],
    )
    assert len(handles) == 1


# ---------------------------------------------------------------------------
# I05  allocate() with multiple in-scope capabilities returns one handle per capability
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_allocate_multiple_capabilities_returns_handle_per_capability(
    async_session,
) -> None:
    """allocate() with N in-scope capabilities returns exactly N InvocationHandles in order."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor(
            "harness-i05", credential_scope=["drive:read", "github:read"]
        ),
    )
    tool_a = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-i05-a",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]}
            ],
        ),
    )
    tool_b = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-i05-b",
            credential_requirements=[
                {"type": "oauth_token", "provider": "github", "scopes": ["github:read"]}
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_a.id, tool_b.id],
    )

    assert len(handles) == 2
    assert handles[0].capability_id == tool_a.id
    assert handles[1].capability_id == tool_b.id


# ---------------------------------------------------------------------------
# I06  allocate() is atomic: one violation rejects the entire allocation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_allocate_is_atomic_single_violation_rejects_all(async_session) -> None:
    """allocate() is atomic: any scope violation rejects the entire batch — no partial grants.

    A harness must not receive handles for in-scope capabilities while the out-of-scope
    one is denied; that would allow partial elevated access.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, ScopeViolationError
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i06", credential_scope=["drive:read"]),
    )
    tool_ok = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-i06-ok",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]}
            ],
        ),
    )
    tool_bad = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-i06-bad",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:write"]}
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    with pytest.raises(ScopeViolationError):
        await allocator.allocate(
            org_id=_ORG_A,
            harness_id=harness_row.id,
            capability_ids=[tool_ok.id, tool_bad.id],
        )

    # Verify no partial allocation was written to the DB
    allocations = await allocator.list_allocations_for_harness(
        org_id=_ORG_A, harness_id=harness_row.id
    )
    assert allocations == [], (
        f"Partial allocations must not persist after scope violation; got {len(allocations)} row(s)"
    )


# ---------------------------------------------------------------------------
# I07  InvocationHandle.capability_id matches the CapabilityDescriptorDB.id
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_invocation_handle_capability_id_matches_db_row(async_session) -> None:
    """The handle's capability_id must be the DB UUID of the capability descriptor row."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i07", credential_scope=["drive:read"]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-i07",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]}
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_row.id],
    )

    assert handles[0].capability_id == tool_row.id


# ---------------------------------------------------------------------------
# I08  InvocationHandle.kind matches the CapabilityDescriptorDB.kind
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_invocation_handle_kind_matches_db_row(async_session) -> None:
    """The handle's kind must match the DescriptorKind of the allocated capability."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i08", credential_scope=[]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-i08", credential_requirements=[]),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_row.id],
    )

    assert handles[0].kind == DescriptorKind.TOOL


# ---------------------------------------------------------------------------
# I09  InvocationHandle.descriptor carries the full JSONB descriptor
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_invocation_handle_descriptor_carries_full_jsonb(async_session) -> None:
    """The handle's descriptor must carry the full JSONB from CapabilityDescriptorDB.descriptor."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i09", credential_scope=[]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-i09", credential_requirements=[]),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_row.id],
    )

    assert handles[0].descriptor["id"] == "tool-i09"
    assert handles[0].descriptor["kind"] == "tool"


# ---------------------------------------------------------------------------
# I10  Allocation is persisted and retrievable via list_allocations_for_harness()
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_allocation_is_persisted_and_retrievable(async_session) -> None:
    """After allocate(), list_allocations_for_harness() returns the allocated capability IDs."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i10", credential_scope=[]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-i10", credential_requirements=[]),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_row.id,
        capability_ids=[tool_row.id],
    )

    allocations = await allocator.list_allocations_for_harness(
        org_id=_ORG_A, harness_id=harness_row.id
    )
    assert len(allocations) >= 1
    allocated_ids = [a.capability_id for a in allocations]
    assert tool_row.id in allocated_ids


# ---------------------------------------------------------------------------
# I11  list_allocations_for_harness() returns [] for a harness with no allocations
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_allocations_empty_for_unallocated_harness(async_session) -> None:
    """list_allocations_for_harness() returns [] for a harness that has no allocations."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-i11", credential_scope=[]),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    allocations = await allocator.list_allocations_for_harness(
        org_id=_ORG_A, harness_id=harness_row.id
    )
    assert allocations == []


# ===========================================================================
# T2-M3 Security tests
# ===========================================================================


@pytest.mark.integration
@pytest.mark.security
async def test_t2m3_narrow_scope_harness_cannot_allocate_write_capability(
    async_session,
) -> None:
    """T2-M3: a harness with read-only scope must not allocate a write-credential capability.

    This is the canonical T2-M3 privilege-escalation test.  A "reader" harness
    (scope: drive:read) that tries to allocate a "writer" tool (requires: drive:write)
    must be rejected at allocation time — no handle is returned, no allocation persisted.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, ScopeViolationError
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    reader_harness = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor(
            "harness-s01-reader",
            credential_scope=["drive:read"],
            name="Reader Harness",
        ),
    )
    writer_tool = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-s01-writer",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:write"]}
            ],
            name="Drive Writer Tool",
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    with pytest.raises(ScopeViolationError) as exc_info:
        await allocator.allocate(
            org_id=_ORG_A,
            harness_id=reader_harness.id,
            capability_ids=[writer_tool.id],
        )
    err = exc_info.value
    assert err.harness_id == reader_harness.id, (
        "ScopeViolationError must identify the violating harness"
    )
    assert err.capability_id == writer_tool.id, (
        "ScopeViolationError must identify the capability that triggered the violation"
    )


@pytest.mark.integration
@pytest.mark.security
async def test_t2m3_zero_scope_harness_cannot_allocate_any_oauth_capability(
    async_session,
) -> None:
    """T2-M3: a harness with no declared scope must not allocate any oauth capability.

    A harness that declares [] as its credential_scope cannot allocate any tool that
    requires an OAuth token — access must be explicitly declared, not implied.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, ScopeViolationError
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    zero_scope_harness = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor(
            "harness-s02-zero",
            credential_scope=[],
            name="Zero-Scope Harness",
        ),
    )
    readonly_tool = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-s02-readonly",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]}
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    with pytest.raises(ScopeViolationError):
        await allocator.allocate(
            org_id=_ORG_A,
            harness_id=zero_scope_harness.id,
            capability_ids=[readonly_tool.id],
        )


@pytest.mark.integration
@pytest.mark.security
async def test_t2m3_scope_violation_error_identifies_blocked_scope(
    async_session,
) -> None:
    """T2-M3: ScopeViolationError identifies the specific blocked scope(s) for the audit trail.

    violating_scopes must contain only the scopes that were rejected, not the full
    harness declared scope.  Precision matters for observability and investigation.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, ScopeViolationError
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-s03", credential_scope=["drive:read"]),
    )
    tool_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-s03",
            credential_requirements=[
                {
                    "type": "oauth_token",
                    "provider": "google",
                    # drive:read is in scope; drive:write is not
                    "scopes": ["drive:read", "drive:write"],
                }
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    with pytest.raises(ScopeViolationError) as exc_info:
        await allocator.allocate(
            org_id=_ORG_A,
            harness_id=harness_row.id,
            capability_ids=[tool_row.id],
        )
    err = exc_info.value
    assert "drive:write" in err.violating_scopes, (
        "ScopeViolationError must identify 'drive:write' as the blocked scope"
    )
    assert "drive:read" not in err.violating_scopes, (
        "Permitted scope 'drive:read' must not appear in violating_scopes (audit precision)"
    )


@pytest.mark.integration
@pytest.mark.security
async def test_t2m3_single_violating_capability_rejects_entire_batch(
    async_session,
) -> None:
    """T2-M3: a single out-of-scope capability rejects the entire allocation batch.

    An attacker cannot extract partial access by padding a malicious capability
    alongside legitimate ones — the entire batch is atomic.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator, ScopeViolationError
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_row = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-s04", credential_scope=["drive:read"]),
    )
    tool_ok_1 = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-s04-ok1", credential_requirements=[]),
    )
    tool_ok_2 = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-s04-ok2",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]}
            ],
        ),
    )
    tool_bad = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor(
            "tool-s04-bad",
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["admin:write"]}
            ],
        ),
    )

    allocator = HarnessCapabilityAllocator(async_session)
    with pytest.raises(ScopeViolationError):
        await allocator.allocate(
            org_id=_ORG_A,
            harness_id=harness_row.id,
            capability_ids=[tool_ok_1.id, tool_ok_2.id, tool_bad.id],
        )

    # No partial handles must be stored in the DB
    allocations = await allocator.list_allocations_for_harness(
        org_id=_ORG_A, harness_id=harness_row.id
    )
    assert allocations == [], (
        f"T2-M3 violation: {len(allocations)} partial allocation(s) persisted after rejection. "
        "No handle may be granted when any capability in the batch violates harness scope."
    )


# ===========================================================================
# Organization isolation tests
# ===========================================================================


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.organization_isolation
async def test_org_isolation_list_allocations_cross_org(async_session) -> None:
    """org_B must not see org_A's capability allocations (ADR-006 tenant isolation).

    Even if org_B supplies org_A's harness_id, list_allocations_for_harness() must
    return zero rows.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_a = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-o01-a", credential_scope=[]),
    )
    tool_a = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-o01-a", credential_requirements=[]),
    )
    allocator = HarnessCapabilityAllocator(async_session)
    await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_a.id,
        capability_ids=[tool_a.id],
    )

    # org_B queries for org_A's harness — must see zero allocations
    org_b_view = await allocator.list_allocations_for_harness(
        org_id=_ORG_B,
        harness_id=harness_a.id,
    )
    assert org_b_view == [], (
        f"org_B saw {len(org_b_view)} row(s) from org_A. "
        "Organisation isolation violation (ADR-006)."
    )


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.organization_isolation
async def test_org_isolation_own_org_allocation_succeeds(async_session) -> None:
    """org_A can allocate and read its own capabilities — isolation must not block same-org ops."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator
    from app.services.capability_registry import CapabilityRegistryService

    registry = CapabilityRegistryService(async_session)

    harness_a = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.HARNESS,
        descriptor=_harness_descriptor("harness-o02-a", credential_scope=[]),
    )
    tool_a = await registry.create(
        org_id=_ORG_A,
        kind=DescriptorKind.TOOL,
        descriptor=_tool_descriptor("tool-o02-a", credential_requirements=[]),
    )
    allocator = HarnessCapabilityAllocator(async_session)
    handles = await allocator.allocate(
        org_id=_ORG_A,
        harness_id=harness_a.id,
        capability_ids=[tool_a.id],
    )

    assert len(handles) == 1, "org_A must be able to allocate its own capabilities"

    allocations = await allocator.list_allocations_for_harness(
        org_id=_ORG_A, harness_id=harness_a.id
    )
    assert len(allocations) >= 1
