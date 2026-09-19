"""Unit: the openapi contract documents the team-run source-draft surface (#1163).

A team run can now be created against a specific team draft version (`team_draft_id` +
`team_draft_version`, required together — a stale version is a 409, a partial pair or a foreign
draft is a 422). `getTeamRun` reads both fields back, always present. A new
`listTeamDraftSucceededVersions` operation lists, per team draft, the distinct versions that have
at least one SUCCEEDED run, newest first. `listTeamDrafts` gains an optional `has_succeeded_run`
filter.

This pins STRUCTURE only (schema properties/required/nullability, the new path's operationId,
parameters and response shape, the has_succeeded_run query parameter, and that the createTeamRun
operation text names the new field and the CONFLICT outcome) — not free prose.

RED until the [impl] PR (I3, I5, I7) adds the two `CreateTeamRunRequest`/`TeamRunRead` properties,
the `has_succeeded_run` query parameter on `listTeamDrafts`, and the new
`/v1/engine/team-drafts/{teamDraftId}/succeeded-versions` path — `openapi/v1.yaml` is untouched by
this [tests] PR.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "v1.yaml"


def _load_spec() -> dict[str, Any]:
    return yaml.safe_load(_SPEC_PATH.read_text(encoding="utf-8"))


def _resolve(spec: dict[str, Any], node: Any) -> Any:
    """Resolve a single ``$ref`` hop against the spec root (the only indirection this spec uses,
    per ``validation_passthrough``'s own docstring and ``test_pagination_contract``'s
    precedent)."""
    if isinstance(node, dict) and "$ref" in node:
        cur: Any = spec
        for key in node["$ref"].lstrip("#/").split("/"):
            cur = cur[key]
        return cur
    return node


def test_create_team_run_request_declares_the_team_draft_pair() -> None:
    spec = _load_spec()
    props = spec["components"]["schemas"]["CreateTeamRunRequest"]["properties"]

    team_draft_id = props["team_draft_id"]
    assert team_draft_id["type"] == "string", team_draft_id
    assert team_draft_id["format"] == "uuid", team_draft_id
    assert team_draft_id.get("nullable") is True, team_draft_id

    team_draft_version = props["team_draft_version"]
    assert team_draft_version["type"] == "integer", team_draft_version
    assert team_draft_version["minimum"] == 1, team_draft_version
    assert team_draft_version.get("nullable") is True, team_draft_version


def test_team_run_read_declares_the_team_draft_pair_as_required_and_nullable() -> None:
    spec = _load_spec()
    schema = spec["components"]["schemas"]["TeamRunRead"]
    required = set(schema.get("required", []))
    assert {"team_draft_id", "team_draft_version"}.issubset(required), schema

    props = schema["properties"]
    assert props["team_draft_id"].get("nullable") is True, props["team_draft_id"]
    assert props["team_draft_version"].get("nullable") is True, props["team_draft_version"]


def test_succeeded_versions_operation_is_documented() -> None:
    spec = _load_spec()
    path = spec["paths"]["/v1/engine/team-drafts/{teamDraftId}/succeeded-versions"]
    op = path["get"]

    assert op["operationId"] == "listTeamDraftSucceededVersions", op

    param_names = {p["name"] for p in op.get("parameters", [])}
    assert {"limit", "offset"}.issubset(param_names), param_names

    assert "404" in op["responses"], op["responses"]

    response_200 = _resolve(spec, op["responses"]["200"])
    schema_200 = _resolve(spec, response_200["content"]["application/json"]["schema"])
    assert schema_200["type"] == "object", schema_200
    required = set(schema_200.get("required", []))
    assert {"team_draft_id", "versions", "total"}.issubset(required), schema_200

    items_schema = _resolve(spec, schema_200["properties"]["versions"]["items"])
    item_required = set(items_schema.get("required", []))
    assert {"version", "team_run_id", "finished_at"}.issubset(item_required), items_schema

    item_props = items_schema["properties"]
    assert item_props["version"]["type"] == "integer", item_props["version"]
    assert item_props["team_run_id"]["format"] == "uuid", item_props["team_run_id"]
    assert item_props["finished_at"]["format"] == "date-time", item_props["finished_at"]


def test_list_team_drafts_has_a_succeeded_run_filter() -> None:
    spec = _load_spec()
    op = spec["paths"]["/v1/engine/team-drafts"]["get"]
    by_name = {p["name"]: p for p in op.get("parameters", [])}
    assert "has_succeeded_run" in by_name, by_name

    schema = _resolve(spec, by_name["has_succeeded_run"]["schema"])
    assert schema["type"] == "boolean", schema


def test_create_team_run_text_names_the_new_field_and_the_conflict_outcome() -> None:
    spec = _load_spec()
    op = spec["paths"]["/v1/engine/team-runs"]["post"]
    text = op.get("summary", "") + " " + op.get("description", "")

    assert "team_draft_version" in text, text
    assert "CONFLICT" in text, text
