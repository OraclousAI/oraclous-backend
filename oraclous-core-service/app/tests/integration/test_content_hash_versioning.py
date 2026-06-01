"""
[tests] content-hash versioning — repository integration tests

Story: ORAA-70 / ORA-73
Architecture refs:
  - OHM v1.0 Spec:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - R2 release page:  https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - Test Strategy:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

Security tier: T3-M2 — content hash enables tamper detection; be-test-reviewer co-sign required.

Behaviours covered:
  C13  create() stores a non-null content_hash without the caller providing one
  C14  two create() calls with identical descriptor dicts produce identical content_hash values
  C15  two create() calls with different descriptor dicts produce different content_hash values
  C16  a single-field change in the descriptor produces a different content_hash
  C17  update_descriptor() recomputes content_hash when the descriptor changes
  C18  update_descriptor() preserves the same content_hash when the descriptor is unchanged
  C19  content_hash is a 64-character lowercase hex string (SHA-256 hex format)

These tests will fail at assertion time until:
  1. ohm.hashing.compute_content_hash is implemented (packages/ohm/hashing.py)
  2. CapabilityDescriptorRepository.create() auto-computes content_hash server-side
  3. CapabilityDescriptorRepository.update_descriptor() recomputes content_hash on change

The current create() stores content_hash=None, so C13/C14/C15/C16/C19 will fail with
AssertionError. That failure is intentional — this file is written test-first (ADR-010).
"""

from __future__ import annotations

import copy
import uuid

import pytest
from app.models.capability_descriptor import DescriptorKind
from app.repositories.capability_descriptor_repository import (
    CapabilityDescriptorRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_ORG = uuid.UUID("cccccccc-0000-0000-0000-000000000003")

_TOOL_DESCRIPTOR: dict = {
    "kind": "tool",
    "id": "hash-versioning-test-tool",
    "version": {"hash": "sha256:placeholder", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Hash Versioning Test Tool",
        "description": "Used to verify content-hash repository integration.",
    },
    "spec": {
        "implementation": {"type": "internal", "handler": "test.HashVersioningTool"},
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
        "output_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        },
        "credential_requirements": [],
    },
}

_SKILL_DESCRIPTOR: dict = {
    "kind": "skill",
    "id": "hash-versioning-test-skill",
    "version": {"hash": "sha256:placeholder", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Hash Versioning Test Skill",
        "description": "Test skill for content-hash repository integration.",
    },
    "spec": {
        "loaded_when": "testing content hash versioning",
        "instructions": "# Test Skill\n\nUsed in integration tests.",
        "capability_requirements": [],
    },
}


# ---------------------------------------------------------------------------
# C13  create() stores a non-null content_hash without the caller providing one
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_stores_non_null_content_hash(async_session: AsyncSession):
    """
    Calling create() without providing content_hash results in a persisted row
    where content_hash is not None — the repository computes it server-side.
    """
    repo = CapabilityDescriptorRepository(async_session)
    row = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=copy.deepcopy(_TOOL_DESCRIPTOR),
    )
    assert row.content_hash is not None
    assert isinstance(row.content_hash, str)
    assert len(row.content_hash) > 0


# ---------------------------------------------------------------------------
# C14  two create() calls with identical descriptor dicts produce identical hashes
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_identical_descriptors_produce_identical_hashes(async_session: AsyncSession):
    """
    Separate writes of the same descriptor content produce the same content_hash
    — the hash is deterministic, not random or timestamp-derived.
    """
    repo = CapabilityDescriptorRepository(async_session)
    row_a = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=copy.deepcopy(_TOOL_DESCRIPTOR),
    )
    row_b = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=copy.deepcopy(_TOOL_DESCRIPTOR),
    )
    assert row_a.content_hash == row_b.content_hash


# ---------------------------------------------------------------------------
# C15  different descriptor dicts produce different content_hash values
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_different_descriptor_kinds_produce_different_hashes(async_session: AsyncSession):
    """Two descriptors with different content produce different content_hash values."""
    repo = CapabilityDescriptorRepository(async_session)
    row_tool = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=copy.deepcopy(_TOOL_DESCRIPTOR),
    )
    row_skill = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.SKILL,
        descriptor=copy.deepcopy(_SKILL_DESCRIPTOR),
    )
    assert row_tool.content_hash != row_skill.content_hash


# ---------------------------------------------------------------------------
# C16  a single-field change in the descriptor produces a different content_hash
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_single_field_change_produces_different_hash(async_session: AsyncSession):
    """
    Changing a single field in the descriptor content produces a different
    content_hash — any field change is detectable via the hash.
    """
    repo = CapabilityDescriptorRepository(async_session)
    row_original = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=copy.deepcopy(_TOOL_DESCRIPTOR),
    )
    modified = copy.deepcopy(_TOOL_DESCRIPTOR)
    modified["metadata"]["name"] = "Modified Descriptor Name"
    row_modified = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=modified,
    )
    assert row_original.content_hash != row_modified.content_hash


# ---------------------------------------------------------------------------
# C17  update_descriptor() recomputes content_hash when the descriptor changes
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_update_descriptor_recomputes_hash_on_change(async_session: AsyncSession):
    """
    After update_descriptor() with changed content, content_hash reflects the
    new descriptor — the hash is not frozen at creation time.
    """
    repo = CapabilityDescriptorRepository(async_session)
    row = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=copy.deepcopy(_TOOL_DESCRIPTOR),
    )
    original_hash = row.content_hash

    updated_descriptor = copy.deepcopy(_TOOL_DESCRIPTOR)
    updated_descriptor["metadata"]["description"] = "Updated description for hash test."
    updated_row = await repo.update_descriptor(row.id, updated_descriptor)

    assert updated_row is not None
    assert updated_row.content_hash is not None
    assert updated_row.content_hash != original_hash


# ---------------------------------------------------------------------------
# C18  update_descriptor() preserves the same content_hash when descriptor unchanged
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_update_descriptor_preserves_hash_when_content_unchanged(async_session: AsyncSession):
    """
    Calling update_descriptor() with the same descriptor content as already
    stored produces the same content_hash — the hash is purely content-derived.
    """
    repo = CapabilityDescriptorRepository(async_session)
    row = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.TOOL,
        descriptor=copy.deepcopy(_TOOL_DESCRIPTOR),
    )
    original_hash = row.content_hash

    updated_row = await repo.update_descriptor(row.id, copy.deepcopy(_TOOL_DESCRIPTOR))

    assert updated_row is not None
    assert updated_row.content_hash == original_hash


# ---------------------------------------------------------------------------
# C19  content_hash is a 64-character lowercase hex string (SHA-256 format)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_content_hash_format_is_sha256_hex(async_session: AsyncSession):
    """
    The stored content_hash is a 64-character lowercase hexadecimal string —
    the canonical encoding of a SHA-256 digest.
    """
    repo = CapabilityDescriptorRepository(async_session)
    row = await repo.create(
        org_id=_ORG,
        kind=DescriptorKind.SKILL,
        descriptor=copy.deepcopy(_SKILL_DESCRIPTOR),
    )
    assert row.content_hash is not None
    assert len(row.content_hash) == 64
    assert all(c in "0123456789abcdef" for c in row.content_hash)
