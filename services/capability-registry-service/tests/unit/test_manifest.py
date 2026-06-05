"""Unit: OHM-v1 descriptor validation (fail-closed on malformed credential requirements)."""

from __future__ import annotations

import pytest
from oraclous_capability_registry_service.domain.errors import InvalidDescriptorError
from oraclous_capability_registry_service.domain.manifest import (
    descriptor_name,
    validate_descriptor,
)
from oraclous_capability_registry_service.models.enums import DescriptorKind

pytestmark = pytest.mark.unit


def _tool(creq: list | None = None) -> dict:
    spec: dict = {"type": "INTERNAL", "capabilities": []}
    if creq is not None:
        spec["credential_requirements"] = creq
    return {"metadata": {"name": "T"}, "spec": spec}


def test_valid_tool_with_oauth_scopes_passes() -> None:
    desc = _tool([{"type": "oauth_token", "provider": "google", "scopes": ["drive.readonly"]}])
    validate_descriptor(DescriptorKind.TOOL, desc)  # no raise


def test_oauth_requirement_without_scopes_is_rejected() -> None:
    desc = _tool([{"type": "oauth_token", "provider": "google", "scopes": []}])
    with pytest.raises(InvalidDescriptorError):
        validate_descriptor(DescriptorKind.TOOL, desc)


def test_oauth_requirement_without_provider_is_rejected() -> None:
    desc = _tool([{"type": "oauth_token", "scopes": ["x"]}])
    with pytest.raises(InvalidDescriptorError):
        validate_descriptor(DescriptorKind.TOOL, desc)


def test_unknown_credential_type_is_rejected() -> None:
    desc = _tool([{"type": "magic_token"}])
    with pytest.raises(InvalidDescriptorError):
        validate_descriptor(DescriptorKind.TOOL, desc)


def test_non_oauth_credential_needs_no_scopes() -> None:
    desc = _tool([{"type": "connection_string"}])
    validate_descriptor(DescriptorKind.TOOL, desc)  # no raise


def test_tool_without_spec_is_rejected() -> None:
    with pytest.raises(InvalidDescriptorError):
        validate_descriptor(DescriptorKind.TOOL, {"metadata": {"name": "x"}})


def test_non_tool_kind_only_requires_an_object() -> None:
    validate_descriptor(DescriptorKind.SKILL, {"anything": True})  # no raise
    with pytest.raises(InvalidDescriptorError):
        validate_descriptor(DescriptorKind.SKILL, ["not", "an", "object"])


def test_descriptor_name_extraction() -> None:
    assert descriptor_name({"metadata": {"name": "Hello"}}) == "Hello"
    assert descriptor_name({"metadata": {}}) is None
    assert descriptor_name({}) is None
