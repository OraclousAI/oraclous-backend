"""Team DAG resolution over OHM v1.1 members[] (issue #395, ADR-031).

``topological_stages`` turns the members' ``depends_on`` edges into ordered execution stages:
members in one stage may run in parallel (fan-out); stages run in sequence (the fan-in barrier).
It fails CLOSED on a cycle, an unknown dependency, or a duplicate role — a malformed team topology
is never silently executed.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.dag import revision_invalidation_set, topological_stages
from oraclous_ohm.errors import OHMDagError
from oraclous_ohm.manifest import OHMManifest, OHMMember


def _m(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref="org:x/a@1", depends_on=deps or [])


def _gate(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="human", human_role="author", depends_on=deps or [])


def test_linear_chain_orders_by_dependency() -> None:
    members = [_m("c", ["b"]), _m("b", ["a"]), _m("a")]
    assert topological_stages(members) == [["a"], ["b"], ["c"]]


def test_independent_members_share_one_stage() -> None:
    members = [_m("a"), _m("b"), _m("synth", ["a", "b"])]
    assert topological_stages(members) == [["a", "b"], ["synth"]]


def test_diamond_dag() -> None:
    members = [_m("a"), _m("b", ["a"]), _m("c", ["a"]), _m("d", ["b", "c"])]
    assert topological_stages(members) == [["a"], ["b", "c"], ["d"]]


def test_single_member() -> None:
    assert topological_stages([_m("only")]) == [["only"]]


# ── ADR-046 (#578): the invalidation set a `revise` re-runs (the gate's producer sub-tree) ──


def test_invalidation_of_a_simple_chain_is_the_direct_producer() -> None:
    # a → gate. Revising the gate re-runs only its producer.
    members = [_m("a"), _gate("gate", ["a"]), _m("c", ["gate"])]
    assert revision_invalidation_set(members, "gate", {}) == {"a"}


def test_invalidation_is_the_transitive_upstream_closure() -> None:
    # root → mid → gate: revising the gate re-runs the whole upstream chain feeding it.
    members = [_m("root"), _m("mid", ["root"]), _gate("gate", ["mid"])]
    assert revision_invalidation_set(members, "gate", {}) == {"root", "mid"}


def test_invalidation_of_a_diamond_covers_both_arms() -> None:
    members = [_m("a"), _m("b", ["a"]), _m("c", ["a"]), _gate("gate", ["b", "c"])]
    assert revision_invalidation_set(members, "gate", {}) == {"a", "b", "c"}


def test_invalidation_stops_at_an_approved_upstream_gate() -> None:
    # a → gate1(APPROVED) → b → gate2. Revising gate2 re-runs only 'b' — gate1 is sealed and 'a'
    # (behind it) is untouched (ADR-046 §2/§5: bounded by the nearest upstream approved gate).
    members = [_m("a"), _gate("gate1", ["a"]), _m("b", ["gate1"]), _gate("gate2", ["b"])]
    inv = revision_invalidation_set(members, "gate2", {"gate1": "approve"})
    assert inv == {"b"}
    assert "a" not in inv and "gate1" not in inv  # sealed behind the approved gate


def test_invalidation_excludes_sibling_branches() -> None:
    # two independent branches feed nothing in common; revising gate-x never touches branch-y.
    members = [
        _m("x1"),
        _gate("gate-x", ["x1"]),
        _m("y1"),
        _gate("gate-y", ["y1"]),
    ]
    assert revision_invalidation_set(members, "gate-x", {}) == {"x1"}  # y1 untouched


def test_invalidation_of_an_unknown_gate_is_empty() -> None:
    assert revision_invalidation_set([_m("a")], "nope", {}) == set()


def test_empty_members() -> None:
    assert topological_stages([]) == []


def test_cycle_fails_closed() -> None:
    with pytest.raises(OHMDagError):
        topological_stages([_m("a", ["b"]), _m("b", ["a"])])


def test_unknown_dependency_fails_closed() -> None:
    with pytest.raises(OHMDagError):
        topological_stages([_m("a", ["ghost"])])


def test_duplicate_role_fails_closed() -> None:
    with pytest.raises(OHMDagError):
        topological_stages([_m("a"), _m("a")])


def test_manifest_execution_stages() -> None:
    manifest = OHMManifest.model_validate(
        {
            "ohm_version": "1.1",
            "metadata": {
                "id": str(uuid.uuid4()),
                "name": "t",
                "owner_organization_id": str(uuid.uuid4()),
                "kind": "team",
            },
            "members": [
                {
                    "role": "researcher",
                    "kind": "agent",
                    "manifest_ref": "org:x/r@1",
                    "depends_on": [],
                },
                {
                    "role": "editor",
                    "kind": "human",
                    "human_role": "lead",
                    "depends_on": ["researcher"],
                },
            ],
            "runtime": {"entrypoint": "researcher"},
        }
    )
    assert manifest.execution_stages() == [["researcher"], ["editor"]]


def test_non_team_manifest_has_no_stages() -> None:
    manifest = OHMManifest.model_validate(
        {
            "ohm_version": "1.0",
            "metadata": {
                "id": str(uuid.uuid4()),
                "name": "solo",
                "owner_organization_id": str(uuid.uuid4()),
            },
            "capabilities": [{"ref": "core/echo@1", "binding": "echo"}],
            "runtime": {"entrypoint": "echo"},
        }
    )
    assert manifest.execution_stages() == []
