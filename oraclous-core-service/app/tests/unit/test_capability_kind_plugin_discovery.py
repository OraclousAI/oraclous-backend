"""
[tests] capability-kind plugin auto-discovery — unit — ORAA-73

Story: ORAA-73 / ORA-71
Architecture refs:
  - Section 7 Portability Story: https://oraclous.atlassian.net/wiki/spaces/OP/pages/753728
  - R2 release page:             https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - Test Strategy:               https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports from app.tools.plugin are done function-locally (inside each test/fixture) per
ADR-010 / CLAUDE.md §4.1 — so pytest collection succeeds and each test fails at runtime with
ImportError (RED-by-design, on its own marker only, never masking other suites).

Until the implementer creates app/tools/plugin.py, every test here fails individually at runtime.

Behaviours covered:
  P01  CapabilityKindPlugin base class is importable from app.tools.plugin
  P02  discover_registered_plugins() is importable and returns an iterable
  P03  plugin_registry.register() is callable
  P04  A mock CapabilityKindPlugin subclass appears in discover_registered_plugins() after register()
  P05  A registered plugin's get_ohm_descriptor() returns a dict containing a 'kind' key
  P13  app/tools/__init__.py does not define the hard-coded _register_all_tools function
       (structural: verifies the reshape occurred — passes only after implementer removes the function)
"""

from __future__ import annotations

import pytest

from app.models.capability_descriptor import DescriptorKind

# ---------------------------------------------------------------------------
# Minimal OHM-format descriptor used by the mock fixture below
# ---------------------------------------------------------------------------

_MOCK_DESCRIPTOR: dict = {
    "kind": "tool",
    "id": "mock-test-tool",
    "version": {"hash": "sha256:test000000", "tags": ["0.0.1"]},
    "metadata": {
        "name": "Mock Test Tool",
        "description": "Fixture-only mock capability for ORAA-73 unit tests.",
    },
    "spec": {
        "implementation": {"type": "internal", "handler": "tests.mock.MockTool"},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
        "credential_requirements": [],
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_plugin():
    """
    Define and register a MockToolPlugin for the duration of one test, then
    unregister it so global plugin_registry state does not bleed across tests.

    The existence of this fixture demonstrates the core ORAA-73 invariant: a new
    capability kind becomes discoverable purely by calling plugin_registry.register()
    — no modification to app/tools/__init__.py or app/tools/factory.py is required.
    """
    from app.tools.plugin import CapabilityKindPlugin, plugin_registry  # function-local: ADR-010

    class MockToolPlugin(CapabilityKindPlugin):
        @classmethod
        def get_ohm_descriptor(cls) -> dict:
            return _MOCK_DESCRIPTOR

        @classmethod
        def get_kind(cls) -> DescriptorKind:
            return DescriptorKind.TOOL

        @classmethod
        def get_plugin_id(cls) -> str:
            return "mock-test-tool"

    plugin_registry.register(MockToolPlugin)
    yield MockToolPlugin
    plugin_registry.unregister(MockToolPlugin)


# ---------------------------------------------------------------------------
# P01  CapabilityKindPlugin is importable from app.tools.plugin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_capability_kind_plugin_is_importable():
    """CapabilityKindPlugin must be importable from app.tools.plugin."""
    from app.tools.plugin import CapabilityKindPlugin as CKP

    assert CKP is not None


# ---------------------------------------------------------------------------
# P02  discover_registered_plugins() is importable and returns an iterable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discover_registered_plugins_returns_iterable():
    """discover_registered_plugins() must be callable and return an iterable."""
    from app.tools.plugin import discover_registered_plugins  # function-local: ADR-010
    result = discover_registered_plugins()
    assert hasattr(result, "__iter__"), (
        "discover_registered_plugins() must return an iterable of CapabilityKindPlugin classes"
    )


# ---------------------------------------------------------------------------
# P03  plugin_registry.register() is callable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plugin_registry_exposes_register():
    """plugin_registry must expose a callable register() method."""
    from app.tools.plugin import plugin_registry  # function-local: ADR-010
    assert callable(getattr(plugin_registry, "register", None)), (
        "plugin_registry must expose a callable register() method for capability kinds "
        "to self-register without modifying core files"
    )


# ---------------------------------------------------------------------------
# P04  A mock plugin appears in discover_registered_plugins() after register()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mock_plugin_discoverable_after_registration(mock_plugin):
    """
    A mock CapabilityKindPlugin registered via plugin_registry.register() must
    appear in discover_registered_plugins() without modifying __init__.py or factory.py.

    This is the central ORAA-73 invariant: zero core-file edits required to add a
    new capability kind to the discoverable set.
    """
    from app.tools.plugin import discover_registered_plugins  # function-local: ADR-010
    discovered_ids = {p.get_plugin_id() for p in discover_registered_plugins()}
    assert "mock-test-tool" in discovered_ids, (
        f"mock-test-tool not found in discovered plugins: {discovered_ids!r}"
    )


# ---------------------------------------------------------------------------
# P05  A registered plugin's get_ohm_descriptor() returns a dict with 'kind'
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registered_plugin_descriptor_has_kind_key(mock_plugin):
    """
    Each plugin returned by discover_registered_plugins() must expose
    get_ohm_descriptor() returning a dict that contains a 'kind' key.
    """
    from app.tools.plugin import discover_registered_plugins  # function-local: ADR-010
    discovered = list(discover_registered_plugins())
    mock = next(
        (p for p in discovered if p.get_plugin_id() == "mock-test-tool"), None
    )
    assert mock is not None, "mock-test-tool plugin was not found in discovered set"
    descriptor = mock.get_ohm_descriptor()
    assert isinstance(descriptor, dict), "get_ohm_descriptor() must return a dict"
    assert "kind" in descriptor, (
        "OHM descriptor must contain a 'kind' key — required by the capability_descriptor schema"
    )


# ---------------------------------------------------------------------------
# P13  app/tools/__init__.py does not define _register_all_tools
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tools_init_does_not_contain_register_all_tools():
    """
    After the ORAA-73 reshape, app/tools/__init__.py must NOT define _register_all_tools.

    The presence of this function is the old hard-coded registration pattern that
    ORAA-73 replaces with plugin auto-discovery.  The test fails until the implementer
    removes the function and wires up the plugin mechanism instead.
    """
    import app.tools as tools_module

    assert not hasattr(tools_module, "_register_all_tools"), (
        "app.tools._register_all_tools still exists.  "
        "The hard-coded registration list has not been replaced by the plugin "
        "discovery mechanism required by ORAA-73."
    )
