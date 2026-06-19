"""OHM v1.1 Team Manifest schema (issue #394, ADR-031).

The v1.1 extension adds team blocks ADDITIVELY: a v1.0 single-entrypoint manifest stays valid and
parses unchanged; the team blocks (members / orchestration / task_board / budget / precedence /
schemas) are only present on a `metadata.kind: team` manifest. These tests pin that contract.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.manifest import (
    OHMBudget,
    OHMManifest,
    OHMMember,
    OHMPrecedence,
)
from pydantic import ValidationError

_ORG = str(uuid.uuid4())
_MID = str(uuid.uuid4())


def _v10_manifest() -> dict:
    """A valid v1.0 single-entrypoint harness — no team blocks at all."""
    return {
        "ohm_version": "1.0",
        "metadata": {"id": _MID, "name": "solo-agent", "owner_organization_id": _ORG},
        "capabilities": [{"ref": "core/echo@1", "binding": "echo"}],
        "runtime": {"entrypoint": "echo"},
    }


def _team_manifest() -> dict:
    """A full v1.1 Team Harness (the ADR-031 contract example, trimmed)."""
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": _MID,
            "name": "market-intel-team",
            "owner_organization_id": _ORG,
            "kind": "team",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/research-agent@3",
                "subgoal": "Gather cited evidence for the assigned sub-topic",
                "depends_on": [],
                "fan_out": {"over": "$.subtopics", "max_parallel": 8},
                "inputs": ["$.objective", "$.window"],
                "outputs_schema": {"$ref": "#/schemas/evidence_batch"},
            },
            {
                "role": "editor",
                "kind": "human",
                "human_role": "domain-lead",
                "subgoal": "Approve the final synthesis",
                "depends_on": ["researcher"],
            },
        ],
        "orchestration": {
            "medium": ["blackboard", "board"],
            "style": "Fan out researchers; barrier; analysts; round-table; escalate to editor.",
            "success_criteria": "Every finding cites >=1 source; 0 unresolved CONTRADICTS.",
            "termination": {
                "max_wall_seconds": 7200,
                "max_rounds": 3,
                "convergence": "evaluator>=0.8",
            },
        },
        "task_board": {
            "columns": ["proposed", "claimed", "in_progress", "blocked", "done", "escalated"]
        },
        "budget": {
            "max_tokens_total": 8_000_000,
            "max_tool_calls_total": 5000,
            "max_sub_runs": 40,
            "max_usd_total": 60,
            "ttl_seconds": 10800,
        },
        "precedence": {"order": ["rules", "bible", "toc", "drafts"], "graph": "derived"},
        "runtime": {"entrypoint": "researcher"},
        "schemas": {"evidence_batch": {"type": "object"}},
    }


# ── additivity: a v1.0 manifest still parses and is NOT a team ──────────────────────────────
def test_v10_manifest_still_parses_unchanged() -> None:
    m = OHMManifest.model_validate(_v10_manifest())
    assert m.metadata.kind == "agent"  # default — v1.0 behaviour
    assert m.is_team() is False
    assert m.members == []
    assert m.orchestration is None
    assert m.task_board is None
    assert m.budget is None
    assert m.precedence is None
    assert m.schemas == {}


# ── a v1.1 team manifest parses with every block ───────────────────────────────────────────
def test_team_manifest_parses_all_blocks() -> None:
    m = OHMManifest.model_validate(_team_manifest())
    assert m.is_team() is True
    assert m.metadata.kind == "team"
    assert len(m.members) == 2

    researcher = m.member_by_role("researcher")
    assert researcher is not None
    assert researcher.manifest_ref == "org:x/research-agent@3"
    assert researcher.fan_out is not None and researcher.fan_out.max_parallel == 8
    assert researcher.outputs_schema == {"$ref": "#/schemas/evidence_batch"}

    editor = m.member_by_role("editor")
    assert editor is not None and editor.depends_on == ["researcher"]

    assert m.orchestration is not None
    assert m.orchestration.medium == ["blackboard", "board"]
    assert m.orchestration.termination.convergence == "evaluator>=0.8"

    assert m.task_board is not None and m.task_board.columns[0] == "proposed"
    assert m.budget is not None and m.budget.max_tokens_total == 8_000_000
    assert m.precedence is not None and m.precedence.order[0] == "rules"
    assert "evidence_batch" in m.schemas


# ── team-pooled budget is the single envelope; all fields optional ─────────────────────────
def test_budget_is_team_pooled_and_optional() -> None:
    b = OHMBudget(max_tokens_total=100, max_sub_runs=5)
    assert b.max_tokens_total == 100
    assert b.max_sub_runs == 5
    assert b.max_usd_total is None  # optional


# ── precedence: graph-as-truth is a MODE, defaulting to derived/disposable ──────────────────
def test_precedence_graph_defaults_to_derived() -> None:
    p = OHMPrecedence(order=["rules", "bible"])
    assert p.graph == "derived"
    assert OHMPrecedence(order=[], graph="authoritative").graph == "authoritative"


# ── a human member structurally REQUIRES a human_role; an agent does not ────────────────────
def test_human_member_requires_human_role() -> None:
    with pytest.raises(ValidationError):
        OHMMember(role="editor", kind="human")  # missing human_role
    ok = OHMMember(role="editor", kind="human", human_role="domain-lead")
    assert ok.human_role == "domain-lead"


def test_agent_member_needs_no_human_role() -> None:
    m = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/a@1")
    assert m.kind == "agent"
    assert m.human_role is None
    assert m.depends_on == []
