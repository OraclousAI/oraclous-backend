"""``sink_roles`` (domain layer; #995) — the ONE shared sink rule the four engine call sites and
``TeamRunOut.answer_roles`` all now share. Pure, no I/O.
"""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.domain.answer_roles import sink_roles
from oraclous_ohm.manifest import OHMFanOut, OHMMember

pytestmark = pytest.mark.unit


def _agent(role: str, deps: list[str] | None = None, fan_out: OHMFanOut | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=deps or [],
        fan_out=fan_out,
    )


def _human(role: str, deps: list[str] | None = None) -> OHMMember:
    return OHMMember(role=role, kind="human", human_role="author", depends_on=deps or [])


def _d(role: str, deps: list[str] | None = None, kind: str = "agent") -> dict:
    return {"role": role, "kind": kind, "depends_on": deps or []}


# ── the straight-line pipeline (researcher -> synthesizer -> linker) ─────────────────────────────


def test_a_three_stage_pipeline_names_only_the_final_sink() -> None:
    members = [
        _agent("researcher"),
        _agent("synthesizer", ["researcher"]),
        _agent("linker", ["synthesizer"]),
    ]
    assert sink_roles(members) == ["linker"]


def test_two_independent_sinks_are_both_listed_in_declaration_order() -> None:
    members = [_agent("a"), _agent("b"), _agent("c", ["a"]), _agent("d", ["b"])]
    assert sink_roles(members) == ["c", "d"]


def test_a_single_member_team_is_its_own_sink() -> None:
    assert sink_roles([_agent("solo")]) == ["solo"]


def test_an_unconsumed_stage_zero_member_is_a_sink() -> None:
    # "a" feeds nothing downstream and nothing depends on it — it is a sink in its own right,
    # alongside the pipeline's real terminal member.
    members = [_agent("a"), _agent("b"), _agent("c", ["b"])]
    assert sink_roles(members) == ["a", "c"]


def test_a_fan_out_sink_is_listed() -> None:
    members = [
        _agent("source"),
        _agent("reporter", ["source"], fan_out=OHMFanOut(over="$.items")),
    ]
    assert sink_roles(members) == ["reporter"]


# ── #995 point 7: a human gate sink is excluded ───────────────────────────────────────────────────


def test_a_human_kind_sink_is_excluded() -> None:
    members = [_agent("drafter"), _human("approve", ["drafter"])]
    assert sink_roles(members) == []


def test_a_human_kind_sink_is_excluded_leaving_the_real_answer_member() -> None:
    members = [_agent("drafter"), _human("gate", ["drafter"]), _agent("publisher", ["gate"])]
    assert sink_roles(members) == ["publisher"]


# ── fail-closed shapes ─────────────────────────────────────────────────────────────────────────


def test_members_not_a_list_yields_empty() -> None:
    assert sink_roles(None) == []
    assert sink_roles({}) == []
    assert sink_roles("members") == []


def test_a_member_without_a_role_yields_empty() -> None:
    assert sink_roles([{"kind": "agent", "depends_on": []}]) == []


def test_depends_on_not_a_list_yields_empty() -> None:
    assert sink_roles([{"role": "a", "kind": "agent", "depends_on": "b"}]) == []


def test_a_cyclic_graph_yields_empty() -> None:
    members = [_d("a", ["b"]), _d("b", ["a"])]
    assert sink_roles(members) == []


def test_raw_dicts_agree_with_ohmmember_objects() -> None:
    obj_members = [_agent("researcher"), _agent("synthesizer", ["researcher"])]
    dict_members = [_d("researcher"), _d("synthesizer", ["researcher"])]
    assert sink_roles(obj_members) == sink_roles(dict_members) == ["synthesizer"]
