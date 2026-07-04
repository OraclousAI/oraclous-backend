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
