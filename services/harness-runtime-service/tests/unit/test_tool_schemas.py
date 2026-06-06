"""Descriptor → ToolSpec marshalling (slice 1): one spec per operation, name + dispatch mapping."""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.tool_schemas import tool_specs_for

pytestmark = pytest.mark.unit

_DESCRIPTOR = {
    "id": "75304fdb-de39-56f2-acd6-915c87934a99",
    "metadata": {"name": "PostgreSQL Reader"},
    "spec": {
        "type": "DATABASE",
        "capabilities": [
            {"name": "list_tables", "description": "List the tables", "parameters": {}},
            {"name": "query", "description": "Run a query", "parameters": {"query": "str"}},
        ],
    },
}


def test_one_spec_per_operation() -> None:
    specs = tool_specs_for("pg", _DESCRIPTOR)
    names = {s.name for s in specs}
    assert names == {"pg__list_tables", "pg__query"}


def test_spec_carries_binding_and_operation_for_dispatch() -> None:
    specs = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}
    assert specs["pg__query"].binding == "pg"
    assert specs["pg__query"].operation == "query"


def test_parameters_become_json_schema() -> None:
    specs = {s.name: s for s in tool_specs_for("pg", _DESCRIPTOR)}
    params = specs["pg__query"].parameters
    assert params["type"] == "object"
    assert params["properties"]["query"]["type"] == "string"


def test_operations_without_a_name_are_skipped() -> None:
    descriptor = {"metadata": {"name": "X"}, "spec": {"capabilities": [{"description": "no name"}]}}
    assert tool_specs_for("x", descriptor) == []
