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
from oraclous_capability_registry_service.domain.plugins.builtin import (
    GitHubReaderPlugin,
    LibraryGroupPlugin,
    MathToolsPlugin,
    MySQLReaderPlugin,
    NotionReaderPlugin,
    PostgreSQLReaderPlugin,
)
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for

pytestmark = pytest.mark.unit

_PLUGINS = plugin_registry.discover()


def _spec_for(plugin_cls: Any, operation: str) -> Any:
    descriptor = plugin_cls.descriptor()
    specs = tool_specs_for(plugin_cls.plugin_id(), descriptor)
    for spec in specs:
        if spec.operation == operation:
            return spec
    raise AssertionError(f"{plugin_cls.NAME} declares no operation {operation!r}")


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


# ── #898 full scope: every shipped operation is strict, and a strict schema is fully closed ──────
#
# Ruled by the owner: ALL first-party built-in operations get the rendered schema and the flag —
# roughly 26 plugins, 38 operations, no exceptions. That INCLUDES the two that declare their own
# ``parameters_schema`` override today, ``core/web-research@1.0.0``'s ``search`` and
# ``core/websearch@1``'s ``search``. Those two need BOTH halves, not just the marker:
# ``_WEB_SEARCH_PARAMETERS_SCHEMA`` (``builtin.py``) declares ``required: ["query"]`` only and
# carries no ``additionalProperties: false``, so a marker alone would leave it partially required
# — which probe fact 2 measured as SILENTLY INERT (7/10 escapes). The marker must be explicit
# rather than inferred from the schema's shape (#901 / #900), and the shape must also be rendered.
# ``test_a_strict_specs_properties_are_all_required_and_the_schema_is_closed`` below is what
# enforces the second half. RED against every currently-shipped plugin: none is strict today.


@pytest.mark.parametrize("plugin_cls", _PLUGINS, ids=[p.NAME for p in _PLUGINS])
def test_every_shipped_plugins_operations_are_strict(plugin_cls: Any) -> None:
    descriptor = plugin_cls.descriptor()
    specs = tool_specs_for(plugin_cls.plugin_id(), descriptor)
    for spec in specs:
        assert spec.strict is True, (plugin_cls.NAME, spec.operation)


@pytest.mark.parametrize("plugin_cls", _PLUGINS, ids=[p.NAME for p in _PLUGINS])
def test_a_strict_specs_properties_are_all_required_and_the_schema_is_closed(
    plugin_cls: Any,
) -> None:
    """The direct encoding of probe fact 2 (a partial ``required`` list makes the provider's
    ``strict`` flag silently inert): wherever a spec ends up strict, EVERY property it declares
    must be in ``required`` and the schema must be closed. This is the one invariant that guards
    all ~38 operations against a future plugin edit reintroducing a partial ``required`` list."""
    descriptor = plugin_cls.descriptor()
    specs = tool_specs_for(plugin_cls.plugin_id(), descriptor)
    for spec in specs:
        if not spec.strict:
            continue
        params = spec.parameters
        assert set(params["properties"]) <= set(params["required"]), (
            plugin_cls.NAME,
            spec.operation,
            params,
        )
        assert params["additionalProperties"] is False, (plugin_cls.NAME, spec.operation)


# ── #898: five plugins that declare no per-argument ``required`` list at all today ────────────────
#
# Each fixture below is the REAL shipped descriptor (``plugin_cls.descriptor()``), not a hand-built
# stand-in — the implementer's job is to add the missing ``required`` entries to the plugin's own
# ``INPUT_SCHEMA`` in ``builtin.py``; these tests fail today for that reason (the argument named
# below is currently OPTIONAL/absent from ``required``, not genuinely mandatory), on top of #898's
# strict-rendering not existing yet.


def test_postgresql_reader_query_is_required_and_params_is_merely_nullable() -> None:
    """``query`` raises in the connector when missing; ``params`` does not — it takes the #898
    nullable-required treatment like any other optional argument, never a bare non-nullable
    requirement."""
    spec = _spec_for(PostgreSQLReaderPlugin, "query")
    assert "query" in spec.parameters["required"]
    assert "query" not in spec.nullable_keys
    assert "params" in spec.parameters["required"]
    assert "params" in spec.nullable_keys


def test_postgresql_reader_list_tables_takes_no_arguments() -> None:
    spec = _spec_for(PostgreSQLReaderPlugin, "list_tables")
    assert spec.parameters["properties"] == {}
    assert spec.parameters["required"] == []


def test_mysql_reader_query_is_required_and_params_is_merely_nullable() -> None:
    spec = _spec_for(MySQLReaderPlugin, "query")
    assert "query" in spec.parameters["required"]
    assert "query" not in spec.nullable_keys
    assert "params" in spec.parameters["required"]
    assert "params" in spec.nullable_keys


def test_mysql_reader_list_tables_takes_no_arguments() -> None:
    spec = _spec_for(MySQLReaderPlugin, "list_tables")
    assert spec.parameters["properties"] == {}
    assert spec.parameters["required"] == []


def test_notion_reader_page_id_is_required_on_read_page() -> None:
    spec = _spec_for(NotionReaderPlugin, "read_page")
    assert "page_id" in spec.parameters["required"]
    assert "page_id" not in spec.nullable_keys


def test_github_reader_repo_is_required_and_path_is_merely_nullable() -> None:
    """``path`` stays optional (nullable) — the connector defaults it to the repository root when
    absent, so it must never become a bare non-nullable requirement."""
    for operation in ("list_files", "read_file"):
        spec = _spec_for(GitHubReaderPlugin, operation)
        assert "repo" in spec.parameters["required"], operation
        assert "repo" not in spec.nullable_keys, operation
        assert "path" in spec.parameters["required"], operation
        assert "path" in spec.nullable_keys, operation


def test_math_tools_every_declared_argument_is_required_and_never_nullable() -> None:
    """Every argument the ``math-tools`` library declares is already rejected as invalid input
    when missing (#822) — none of them is genuinely optional, so none should end up in
    ``nullable_keys`` even though the strict schema puts all of them in ``required``."""
    descriptor = MathToolsPlugin.descriptor()
    specs = tool_specs_for(MathToolsPlugin.plugin_id(), descriptor)
    assert specs
    for spec in specs:
        for key in spec.parameters["properties"]:
            assert key in spec.parameters["required"], (spec.operation, key)
            assert key not in spec.nullable_keys, (spec.operation, key)


def test_library_group_every_declared_argument_is_required_and_never_nullable() -> None:
    descriptor = LibraryGroupPlugin.descriptor()
    specs = tool_specs_for(LibraryGroupPlugin.plugin_id(), descriptor)
    assert specs
    for spec in specs:
        for key in spec.parameters["properties"]:
            assert key in spec.parameters["required"], (spec.operation, key)
            assert key not in spec.nullable_keys, (spec.operation, key)
