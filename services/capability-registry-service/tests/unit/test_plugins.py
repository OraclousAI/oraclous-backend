"""Unit: built-in tool plugins are discoverable and produce valid OHM descriptors."""

from __future__ import annotations

import uuid

import pytest
from oraclous_capability_registry_service.domain.manifest import validate_descriptor
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.models.enums import DescriptorKind

pytestmark = pytest.mark.unit


def test_builtin_plugins_are_registered() -> None:
    ids = {p.plugin_id() for p in plugin_registry.discover()}
    # the five connector readers seeded in S2
    assert len(ids) >= 5


def test_every_plugin_descriptor_is_valid_and_id_consistent() -> None:
    for plugin in plugin_registry.discover():
        desc = plugin.descriptor()
        assert plugin.kind() is DescriptorKind.TOOL
        # plugin_id matches the descriptor's embedded id and is a valid UUID
        assert desc["id"] == plugin.plugin_id()
        uuid.UUID(desc["id"])
        # the descriptor passes OHM-v1 validation (oauth requirements carry scopes, etc.)
        validate_descriptor(plugin.kind(), desc)
        assert desc["metadata"]["name"]
        assert isinstance(desc["spec"]["capabilities"], list)


def test_discover_is_order_stable() -> None:
    first = [p.plugin_id() for p in plugin_registry.discover()]
    second = [p.plugin_id() for p in plugin_registry.discover()]
    assert first == second == sorted(first)
