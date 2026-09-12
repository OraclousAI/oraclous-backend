"""#911 review addition: run the projection against every REAL shipped plugin descriptor.

Every other fixture in this suite is hand-built, so nothing catches a future edit to a shipped
plugin (``services/capability-registry-service/.../domain/plugins/builtin.py``) that breaks what a
model is actually told about a real, in-production tool. This file closes that gap by building a
``ToolSpec`` for every operation of every currently-registered plugin.

Cross-service import, deliberately: ``harness-runtime-service`` (Layer 3) importing
``capability-registry-service`` (Layer 2) is a DOWNWARD import under ADR-001's four-layer contract
(the root ``pyproject.toml``'s import-linter "Four-layer architecture" contract lists
``oraclous_harness_runtime_service`` above ``oraclous_capability_registry_service``) — this is not
a layering violation, and the package is already an installed workspace member.

Assertions are kept GENERAL — never pinning one plugin's exact schema — so this never becomes a
chore every time a plugin's ``INPUT_SCHEMA`` changes; a specific plugin's fixed shape is already
covered exhaustively elsewhere in this test suite with hand-built fixtures.

HONEST NOTE ON RED VS GREEN: at the time of writing, every one of the (currently 26 — not the ~27
the original #911 ruling comment estimated elsewhere; a moving number, not asserted here) shipped
plugins already passes every check below. This is NOT because no plugin could ever fail it — it
is because the current ``_project_input_schema`` implementation builds ``required`` as a subset of
the exact same key set ``properties`` is built from, so "every required name is also a properties
key" and "operation is excluded from both" hold BY CONSTRUCTION for any descriptor today, hostile
or real. This test does not currently catch a live defect in shipped data; it is kept as the
highest-value regression guard against a FUTURE plugin edit, or a future change to the projection
that weakens that invariant — the review asked for it explicitly for that reason, and a guard that
passes today is not a reason to skip writing it.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_capability_registry_service.domain.plugins import builtin  # noqa: F401  (registers)
from oraclous_capability_registry_service.domain.plugins.base import plugin_registry
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for

pytestmark = pytest.mark.unit

_PLUGINS = plugin_registry.discover()


@pytest.mark.parametrize("plugin_cls", _PLUGINS, ids=[p.NAME for p in _PLUGINS])
def test_every_shipped_plugins_operations_produce_a_usable_spec(plugin_cls: Any) -> None:
    """For every operation of every shipped plugin:

    - building the spec must not raise;
    - ``operation`` must appear in neither ``properties`` nor ``required``;
    - every name in ``required`` must also be a ``properties`` key (a mandatory argument the model
      is never shown is unfillable by construction);
    - ``properties`` must be a dict of dicts (a schema object per property, not a bare value).
    """
    descriptor = plugin_cls.descriptor()
    specs = tool_specs_for(plugin_cls.plugin_id(), descriptor)
    for spec in specs:
        params = spec.parameters
        properties = params.get("properties")
        required = params.get("required")
        assert isinstance(properties, dict), (plugin_cls.NAME, spec.operation, properties)
        assert all(isinstance(v, dict) for v in properties.values()), (
            plugin_cls.NAME,
            spec.operation,
            properties,
        )
        assert "operation" not in properties, (plugin_cls.NAME, spec.operation)
        assert isinstance(required, list)
        assert "operation" not in required, (plugin_cls.NAME, spec.operation)
        for name in required:
            assert name in properties, (
                f"{plugin_cls.NAME}.{spec.operation}: {name!r} is required but not in "
                f"properties — unfillable by construction. properties={properties}"
            )
