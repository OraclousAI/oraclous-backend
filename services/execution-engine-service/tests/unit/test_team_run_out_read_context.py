"""TeamRunOut read-side context — graph_id + team_name (#638 concern 2) — unit, no DB.

The console's Results tab needs the run's bound ``graph_id`` (which workspace to read artifacts
from) and its ``team_name`` (run-again resolves the draft by name). Both are additive on the detail
read: ``graph_id`` is a direct column; ``team_name`` is dug from the stored manifest.metadata.name
(like the #634 list row). The raw manifest is NEVER serialized (it is an excluded source field).
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_execution_engine_service.schema.engine_schemas import TeamRunOut

pytestmark = pytest.mark.unit


class _Row:
    """A run row shaped like the ORM object (attribute access is all model_validate uses)."""

    def __init__(self, **kw: object) -> None:
        self.id = uuid.uuid4()
        self.organisation_id = uuid.uuid4()
        self.state = "SUCCEEDED"
        self.results = {}
        self.paused_at = []
        self.error_message = None
        self.created_at = None
        self.graph_id = kw.get("graph_id")
        self.manifest = kw.get("manifest")


def test_graph_id_and_team_name_are_carried_on_the_read() -> None:
    out = TeamRunOut.model_validate(
        _Row(graph_id="graph-123", manifest={"metadata": {"name": "book-studio"}})
    )
    assert out.graph_id == "graph-123"
    assert out.team_name == "book-studio"  # dug from manifest.metadata.name


def test_the_raw_manifest_is_never_serialized() -> None:
    out = TeamRunOut.model_validate(
        _Row(graph_id=None, manifest={"metadata": {"name": "t"}, "members": [{"role": "a"}]})
    )
    dumped = out.model_dump()
    assert "manifest" not in dumped  # the excluded source field never leaks into the response
    assert dumped["team_name"] == "t" and dumped["graph_id"] is None


def test_missing_manifest_or_name_degrades_to_none() -> None:
    assert TeamRunOut.model_validate(_Row(manifest=None)).team_name is None
    assert TeamRunOut.model_validate(_Row(manifest={})).team_name is None
    assert TeamRunOut.model_validate(_Row(manifest={"metadata": {}})).team_name is None


# ── #642: grounding_score rides both read surfaces ────────────────────────────────────────────
# RED until the [impl] adds the column + the response fields. Shown next to the run's cost so a
# green state can never hide an ungrounded run ("3,099 tokens billed, zero claims grounded").


def test_grounding_score_is_carried_on_the_detail_read() -> None:
    row = _Row(manifest=None)
    row.grounding_score = 0.6
    assert TeamRunOut.model_validate(row).grounding_score == 0.6


def test_grounding_score_defaults_to_none_for_pre_642_rows() -> None:
    assert TeamRunOut.model_validate(_Row(manifest=None)).grounding_score is None


# ── #995: answer_roles — the plan's sink members, derived read-side off the stored manifest ────


def test_answer_roles_is_carried_in_the_dump_unlike_the_raw_manifest() -> None:
    manifest = {"members": [{"role": "a", "kind": "agent", "depends_on": []}]}
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    dumped = out.model_dump()
    assert "manifest" not in dumped
    assert dumped["answer_roles"] == ["a"]


def test_answer_roles_names_the_final_sink_of_a_pipeline() -> None:
    manifest = {
        "members": [
            {"role": "researcher", "kind": "agent", "depends_on": []},
            {"role": "synthesizer", "kind": "agent", "depends_on": ["researcher"]},
            {"role": "linker", "kind": "agent", "depends_on": ["synthesizer"]},
        ]
    }
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    assert out.answer_roles == ["linker"]


def test_answer_roles_lists_two_independent_sinks_in_declaration_order() -> None:
    manifest = {
        "members": [
            {"role": "a", "kind": "agent", "depends_on": []},
            {"role": "b", "kind": "agent", "depends_on": []},
            {"role": "c", "kind": "agent", "depends_on": ["a"]},
            {"role": "d", "kind": "agent", "depends_on": ["b"]},
        ]
    }
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    assert out.answer_roles == ["c", "d"]


def test_answer_roles_for_a_single_member_team_is_that_member() -> None:
    manifest = {"members": [{"role": "solo", "kind": "agent", "depends_on": []}]}
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    assert out.answer_roles == ["solo"]


def test_answer_roles_excludes_a_human_kind_sink() -> None:
    manifest = {
        "members": [
            {"role": "drafter", "kind": "agent", "depends_on": []},
            {"role": "approve", "kind": "human", "depends_on": ["drafter"]},
        ]
    }
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    assert out.answer_roles == []


def test_answer_roles_includes_an_unconsumed_stage_zero_member() -> None:
    manifest = {
        "members": [
            {"role": "a", "kind": "agent", "depends_on": []},
            {"role": "b", "kind": "agent", "depends_on": []},
            {"role": "c", "kind": "agent", "depends_on": ["b"]},
        ]
    }
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    assert out.answer_roles == ["a", "c"]


def test_answer_roles_lists_a_fan_out_sink() -> None:
    manifest = {
        "members": [
            {"role": "source", "kind": "agent", "depends_on": []},
            {
                "role": "reporter",
                "kind": "agent",
                "depends_on": ["source"],
                "fan_out": {"over": "$.items"},
            },
        ]
    }
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    assert out.answer_roles == ["reporter"]


def test_answer_roles_lists_a_role_absent_from_results() -> None:
    # a listed sink may have failed/been skipped — it is still named (#995 point 8); `results`
    # never gates membership, only whether a caller finds a payload for the named role.
    manifest = {
        "members": [
            {"role": "a", "kind": "agent", "depends_on": []},
            {"role": "b", "kind": "agent", "depends_on": ["a"]},
        ]
    }
    row = _Row(manifest=manifest)
    row.results = {"a": {"summary": "done"}}  # "b" never produced a result
    out = TeamRunOut.model_validate(row)
    assert out.answer_roles == ["b"]


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {},
        {"members": "not-a-list"},
        {"members": [{"kind": "agent", "depends_on": []}]},  # no role
        {"members": [{"role": "a", "kind": "agent", "depends_on": "b"}]},  # depends_on not a list
        {  # cyclic
            "members": [
                {"role": "a", "kind": "agent", "depends_on": ["b"]},
                {"role": "b", "kind": "agent", "depends_on": ["a"]},
            ]
        },
    ],
)
def test_answer_roles_fails_closed_to_empty_without_raising(manifest: object) -> None:
    out = TeamRunOut.model_validate(_Row(manifest=manifest))
    assert out.answer_roles == []


def test_answer_roles_is_absent_from_the_list_item() -> None:
    from oraclous_execution_engine_service.schema.engine_schemas import TeamRunListItem

    assert "answer_roles" not in TeamRunListItem.model_fields


def test_status_out_carries_grounding_score_next_to_cost() -> None:
    from datetime import UTC, datetime

    from oraclous_execution_engine_service.schema.engine_schemas import (
        TeamRunCost,
        TeamRunStatusOut,
    )

    out = TeamRunStatusOut(
        team_run_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        healthy=True,
        state="SUCCEEDED",
        progress=100,
        last_run_at=datetime(2026, 7, 27, tzinfo=UTC),
        last_outcome="SUCCEEDED",
        cost=TeamRunCost(tokens=3099),
        grounding_score=0.6,
    )
    dumped = out.model_dump()
    assert dumped["grounding_score"] == 0.6
    assert dumped["cost"]["tokens"] == 3099
