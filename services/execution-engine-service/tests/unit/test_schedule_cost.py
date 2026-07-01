"""Cadence-aware cost pre-flight projection (#603, ADR-048 dec-4(a)) — the PURE domain core.

Deterministic, no I/O, no DB, no network. Exercises: fires/day per cadence (weekly never rounds to
0), exact per-Mtok USD math, the fail-closed UNPRICED contract (never a fabricated $0), binding
precedence (inline sub-harness > team model > cheaper scan default), worst-case full-pool sum, and a
mixed priced/unpriced fleet.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_execution_engine_service.domain.schedule_cost import (
    SCHEDULED_SCAN_DEFAULT_BINDING,
    apply_scheduled_scan_default,
    fires_per_day,
    project_schedule_cost,
)
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_MINI = "openrouter/openai/gpt-4o-mini"  # 0.15/0.60 per Mtok
_SONNET = "openrouter/anthropic/claude-3.5-sonnet"  # 3.00/15.00 per Mtok
_UNKNOWN = "openrouter/acme/secret-model"  # absent from RATES → unpriced


def _agent(role: str, deps: list[str] | None = None) -> dict:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
        "tools": [],
    }


def _team(members: list[dict], models: list[dict] | None = None) -> object:
    doc: dict = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "team",
            "owner_organization_id": _ORG,
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }
    if models is not None:
        doc["models"] = models
    return load_ohm(doc)


def _sub(binding: str) -> dict:
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "s", "owner_organization_id": _ORG},
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "models": [{"role": "primary", "binding": binding, "protocol_shape": "openai-compatible"}],
        "runtime": {"entrypoint": "primary"},
    }


# ── fires_per_day ─────────────────────────────────────────────────────────────────────────────────
def test_fires_per_day_per_cadence() -> None:
    assert fires_per_day("0 * * * *") == pytest.approx(24.0)  # hourly
    assert fires_per_day("0 9 * * *") == pytest.approx(1.0)  # daily
    assert fires_per_day("*/5 * * * *") == pytest.approx(288.0)  # every 5 min
    # weekly is a FLOAT ~0.143/day — never rounded to 0 (4 fires in the 28-day window)
    assert fires_per_day("0 9 * * 1") == pytest.approx(4 / 28)


def test_fires_per_day_invalid_cron_raises() -> None:
    with pytest.raises(ValueError, match="invalid cron"):
        fires_per_day("not a cron")


# ── the projection ────────────────────────────────────────────────────────────────────────────────
def test_single_priced_member_exact_usd_math() -> None:
    team = _team([_agent("a")])
    # gpt-4o-mini @ 1M in + 1M out = 0.15 + 0.60 = 0.75 usd/fire; daily → 0.75 usd/day
    proj = project_schedule_cost(
        team, {"a": _sub(_MINI)}, "0 9 * * *", expected_in=1_000_000, expected_out=1_000_000
    )
    assert proj.cadence_fires_per_day == pytest.approx(1.0)
    assert proj.per_member[0].priced is True
    assert proj.per_member[0].usd_per_fire == pytest.approx(0.75)
    assert proj.per_member[0].usd_per_day == pytest.approx(0.75)
    assert proj.fleet_usd_per_day == pytest.approx(0.75)
    assert proj.unpriced_members == []


def test_unknown_binding_is_unpriced_never_zero_dollars() -> None:
    team = _team([_agent("a")])
    proj = project_schedule_cost(
        team, {"a": _sub(_UNKNOWN)}, "0 * * * *", expected_in=1_000_000, expected_out=1_000_000
    )
    m = proj.per_member[0]
    assert m.priced is False and m.usd_per_fire is None and m.usd_per_day is None  # NOT $0
    assert proj.unpriced_members == ["a"]
    assert proj.fleet_usd_per_day == pytest.approx(0.0)  # the unpriced member adds 0, not a guess


def test_unset_member_with_default_disabled_is_unpriced() -> None:
    # a manifest_ref-only member (no sub-harness, no team model); default disabled → unpriced.
    team = _team([_agent("a")])
    proj = project_schedule_cost(
        team, {}, "0 9 * * *", expected_in=1000, expected_out=1000, default_binding=None
    )
    assert proj.per_member[0].binding is None and proj.per_member[0].priced is False
    assert proj.unpriced_members == ["a"]


def test_declared_model_in_a_malformed_sub_is_not_understated_to_the_default() -> None:
    # review #603 MED: a sub that DECLARES an expensive model but is otherwise malformed (here: no
    # metadata, so a full load_ohm would raise) must still price at the DECLARED model, never
    # drop to the cheaper scan default and UNDERSTATE the pre-flight (the wrong direction for a cost
    # estimate). The binding is read straight off the sub dict, not via load_ohm.
    malformed_but_declares_sonnet = {
        "ohm_version": "1.0",
        "models": [{"role": "primary", "binding": _SONNET, "protocol_shape": "openai-compatible"}],
        # NB: no metadata / prompts / actors / runtime → a full OHM parse would reject it
    }
    team = _team([_agent("a")])
    proj = project_schedule_cost(
        team,
        {"a": malformed_but_declares_sonnet},
        "0 9 * * *",
        expected_in=1_000_000,
        expected_out=1_000_000,
    )
    # sonnet @ 1M+1M = 3.00 + 15.00 = 18.00/day — the DECLARED model, NOT gemini's 0.375
    assert proj.per_member[0].binding == _SONNET
    assert proj.per_member[0].usd_per_day == pytest.approx(18.00)


def test_binding_resolution_precedence() -> None:
    # inline sub-harness primary_model WINS over team model_by_role; team model wins over default.
    team = _team(
        [_agent("a"), _agent("b")],
        models=[{"role": "b", "binding": _SONNET, "protocol_shape": "openai-compatible"}],
    )
    proj = project_schedule_cost(
        team, {"a": _sub(_MINI)}, "0 9 * * *", expected_in=1_000_000, expected_out=1_000_000
    )
    by_role = {m.role: m for m in proj.per_member}
    assert by_role["a"].binding == _MINI  # sub-harness wins
    assert by_role["b"].binding == _SONNET  # team model_by_role (no sub-harness) wins over default


def test_worst_case_full_pool_sum() -> None:
    # every agent member fires every window; fleet = sum of per-member usd/day (worst case)
    team = _team([_agent("a"), _agent("b"), _agent("c")])
    subs = {"a": _sub(_MINI), "b": _sub(_MINI), "c": _sub(_MINI)}
    proj = project_schedule_cost(
        team, subs, "0 9 * * *", expected_in=1_000_000, expected_out=1_000_000
    )
    assert proj.fleet_usd_per_day == pytest.approx(0.75 * 3)
    assert len(proj.per_member) == 3 and proj.unpriced_members == []


def test_mixed_fleet_sums_priced_lists_unpriced() -> None:
    team = _team([_agent("a"), _agent("b")])
    subs = {"a": _sub(_MINI), "b": _sub(_UNKNOWN)}
    proj = project_schedule_cost(
        team, subs, "0 9 * * *", expected_in=1_000_000, expected_out=1_000_000
    )
    assert proj.fleet_usd_per_day == pytest.approx(0.75)  # only the priced member
    assert proj.unpriced_members == ["b"]


def test_human_member_is_skipped_not_scan_defaulted() -> None:
    # a human gate incurs no LLM cost — it must NOT be priced at the scan default (which would
    # fabricate an LLM cost for a human). It is skipped entirely.
    team = _team(
        [
            _agent("a"),
            {"role": "rev", "kind": "human", "human_role": "reviewer", "depends_on": ["a"]},
        ]
    )
    proj = project_schedule_cost(
        team, {"a": _sub(_MINI)}, "0 9 * * *", expected_in=1_000_000, expected_out=1_000_000
    )
    assert [m.role for m in proj.per_member] == ["a"]  # the human is absent from the projection
    assert proj.fleet_usd_per_day == pytest.approx(0.75) and proj.unpriced_members == []


# ── 4(c): the cheaper scheduled-scan tier default (apply_scheduled_scan_default) ──────────────
def _bare_sub() -> dict:
    # a valid single-agent sub-harness with NO model declared (the 4(c) target).
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "s", "owner_organization_id": _ORG},
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }


def _binding_of(sub: dict) -> str | None:
    models = sub.get("models") or []
    return next((m.get("binding") for m in models if m.get("binding")), None)


def test_scan_default_stamps_an_unset_member() -> None:
    manifest = {"members": [_agent("a")]}
    _m, subs = apply_scheduled_scan_default(manifest, {"a": _bare_sub()})
    assert _binding_of(subs["a"]) == SCHEDULED_SCAN_DEFAULT_BINDING


def test_scan_default_declared_binding_wins_untouched() -> None:
    manifest = {"members": [_agent("a")]}
    declared = _sub(_SONNET)
    _m, subs = apply_scheduled_scan_default(manifest, {"a": declared})
    assert _binding_of(subs["a"]) == _SONNET  # a declared model is never downgraded


def test_scan_default_skips_human_and_missing_subharness() -> None:
    # a human gate is never LLM-defaulted; a manifest_ref member (no sub-harness) is untouched.
    manifest = {
        "members": [
            _agent("a"),
            {"role": "rev", "kind": "human", "human_role": "reviewer", "depends_on": ["a"]},
        ]
    }
    _m, subs = apply_scheduled_scan_default(manifest, {})  # 'a' has no sub-harness → out of reach
    assert subs == {}  # nothing stamped (no inline sub to stamp; human never stamped)


def test_scan_default_does_not_mutate_the_caller_dicts() -> None:
    manifest = {"members": [_agent("a")]}
    original = _bare_sub()
    subs_in = {"a": original}
    _m, subs_out = apply_scheduled_scan_default(manifest, subs_in)
    assert _binding_of(original) is None  # the ORIGINAL sub-harness is unchanged (per-fire copy)
    assert _binding_of(subs_out["a"]) == SCHEDULED_SCAN_DEFAULT_BINDING  # only the copy is stamped


def test_scan_default_stamped_member_is_priceable_in_the_projection() -> None:
    # the fired default must be priceable — a preflight over the stamped subs prices it, not
    # "unpriced" (the whole point: the cheaper tier the fleet runs on has a known cost).
    manifest_obj = _team([_agent("a")])
    _m, subs = apply_scheduled_scan_default({"members": [_agent("a")]}, {"a": _bare_sub()})
    proj = project_schedule_cost(manifest_obj, subs, "0 9 * * *")
    assert proj.per_member[0].priced is True and proj.unpriced_members == []
