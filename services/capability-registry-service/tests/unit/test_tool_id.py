"""Unit: deterministic tool id generation (stable across runs, sensitive to identity)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_capability_registry_service.domain.tool_id import generate_tool_id

pytestmark = pytest.mark.unit


def test_same_identity_same_id() -> None:
    a = generate_tool_id("Google Drive Reader", "1.0.0", "INGESTION")
    b = generate_tool_id("Google Drive Reader", "1.0.0", "INGESTION")
    assert a == b
    assert isinstance(a, uuid.UUID)


def test_version_changes_the_id() -> None:
    assert generate_tool_id("X", "1.0.0", "C") != generate_tool_id("X", "2.0.0", "C")


def test_name_is_case_and_space_insensitive() -> None:
    assert generate_tool_id("Google Drive Reader") == generate_tool_id("google-drive-reader")


def test_namespace_scopes_the_id() -> None:
    base = generate_tool_id("X", "1.0.0", "C")
    scoped = generate_tool_id("X", "1.0.0", "C", namespace="org-123")
    assert base != scoped
