"""Unit: instance readiness status derivation + required-credential extraction."""

from __future__ import annotations

import pytest
from oraclous_capability_registry_service.domain.manifest import required_credential_types
from oraclous_capability_registry_service.models.enums import InstanceStatus
from oraclous_capability_registry_service.services.instance_manager import _status_for

pytestmark = pytest.mark.unit


def test_required_credential_types_dedup_and_required_only() -> None:
    spec = {
        "spec": {
            "credential_requirements": [
                {"type": "oauth_token", "provider": "google", "scopes": ["x"]},
                {"type": "oauth_token", "provider": "google", "scopes": ["x"]},
                {"type": "api_key", "provider": "notion", "required": False},
            ]
        }
    }
    assert required_credential_types(spec) == ["oauth_token"]


def test_required_credential_types_empty_when_none() -> None:
    assert required_credential_types({"spec": {"capabilities": []}}) == []
    assert required_credential_types({}) == []


def test_status_ready_when_nothing_required() -> None:
    assert _status_for([], {}) is InstanceStatus.READY


def test_status_configuration_required_when_unmapped() -> None:
    assert _status_for(["oauth_token"], {}) is InstanceStatus.CONFIGURATION_REQUIRED


def test_status_ready_when_all_mapped() -> None:
    assert _status_for(["oauth_token"], {"oauth_token": "cred-1"}) is InstanceStatus.READY
