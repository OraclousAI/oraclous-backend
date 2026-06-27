"""#576 — user-configurable per-member token/iteration cap (OHM layer).

The hardcoded policy tier (development-default = 200k) is the per-member runtime cap today, and a
user cannot raise it — heavy agents (fact-checker, isbn-advisor) fail with "token budget exhausted"
that is Oraclous's own cap, not the user's OpenRouter limit. This makes the per-member cap
user-settable WITHOUT reviving ADR-031's rejected Alternative C: the field is a per-member SAFETY
CAP (a sub-ceiling on one member's loop), NOT a per-member budget surface. The team-pooled
``max_tokens_total`` stays the single keystone ceiling, and a per-member cap is always clamped to
≤ the pooled total, so the aggregate can never escape it (ADR-031 keystone preserved).

RED until the [impl] adds the OHMMember / OHMBudget fields + ``resolve_member_caps``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ── the user-settable fields ─────────────────────────────────────────────────────────────────


def test_member_accepts_optional_per_member_caps() -> None:
    from oraclous_ohm.manifest import OHMMember

    m = OHMMember(role="fact-checker", kind="agent", manifest_ref="org:x/a@1", max_tokens=500_000)
    assert m.max_tokens == 500_000
    assert m.max_tool_calls is None  # independent + optional


def test_member_caps_default_to_none_back_compat() -> None:
    from oraclous_ohm.manifest import OHMMember

    m = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/a@1")
    assert m.max_tokens is None and m.max_tool_calls is None  # unchanged manifests parse as before


def test_member_caps_reject_non_positive() -> None:
    from oraclous_ohm.manifest import OHMMember

    with pytest.raises(ValueError):
        OHMMember(role="x", kind="agent", manifest_ref="org:x/a@1", max_tokens=0)
    with pytest.raises(ValueError):
        OHMMember(role="x", kind="agent", manifest_ref="org:x/a@1", max_tool_calls=-1)


def test_budget_accepts_team_wide_per_member_defaults() -> None:
    from oraclous_ohm.manifest import OHMBudget

    b = OHMBudget(
        max_tokens_total=8_000_000, max_tokens_per_member=400_000, max_tool_calls_per_member=120
    )
    assert b.max_tokens_per_member == 400_000
    assert b.max_tool_calls_per_member == 120
    # the pooled keystone field is untouched (ADR-031 — the single governed ceiling)
    assert b.max_tokens_total == 8_000_000


# ── resolve_member_caps — precedence + the keystone clamp ─────────────────────────────────────


def test_member_override_beats_team_default() -> None:
    from oraclous_ohm.manifest import OHMBudget, OHMMember, resolve_member_caps

    member = OHMMember(
        role="fact-checker", kind="agent", manifest_ref="org:x/a@1", max_tokens=600_000
    )
    budget = OHMBudget(max_tokens_per_member=300_000, max_tool_calls_per_member=80)
    max_tokens, max_tool_calls = resolve_member_caps(member, budget)
    assert max_tokens == 600_000  # the member's own override wins
    assert max_tool_calls == 80  # falls through to the team default (no member override)


def test_team_default_applies_when_no_member_override() -> None:
    from oraclous_ohm.manifest import OHMBudget, OHMMember, resolve_member_caps

    member = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/a@1")
    budget = OHMBudget(max_tokens_per_member=350_000)
    max_tokens, max_tool_calls = resolve_member_caps(member, budget)
    assert max_tokens == 350_000
    assert max_tool_calls is None  # neither set → None → enforcement falls back to the policy tier


def test_none_when_neither_set_falls_back_to_policy() -> None:
    from oraclous_ohm.manifest import OHMMember, resolve_member_caps

    member = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/a@1")
    # no budget at all (a v1.0 team) → both None → the policy tier remains the cap (back-compat)
    assert resolve_member_caps(member, None) == (None, None)


def test_per_member_cap_is_clamped_to_the_pooled_total_keystone() -> None:
    # ADR-031 keystone: a per-member SAFETY CAP is a sub-ceiling that can never escape the
    # team-pooled total. A member asking for more than the whole team's pool is clamped DOWN to
    # the pool — the aggregate stays bounded (Alternative C's failure mode cannot occur).
    from oraclous_ohm.manifest import OHMBudget, OHMMember, resolve_member_caps

    member = OHMMember(role="greedy", kind="agent", manifest_ref="org:x/a@1", max_tokens=10_000_000)
    budget = OHMBudget(max_tokens_total=1_000_000)
    max_tokens, _ = resolve_member_caps(member, budget)
    assert max_tokens == 1_000_000  # clamped to the pooled keystone, not the member's 10M


def test_clamp_is_a_noop_when_under_the_pool() -> None:
    from oraclous_ohm.manifest import OHMBudget, OHMMember, resolve_member_caps

    member = OHMMember(role="ok", kind="agent", manifest_ref="org:x/a@1", max_tokens=400_000)
    budget = OHMBudget(max_tokens_total=8_000_000, max_tokens_per_member=300_000)
    max_tokens, _ = resolve_member_caps(member, budget)
    assert max_tokens == 400_000  # well under the pool → the member's value stands


def test_member_override_binds_standalone_with_no_team_budget() -> None:
    # the case the #583 review doubted: a member's OWN cap is enforced even with NO team budget
    # block. Both deployed proofs (member max_tokens, budget=None) bit — this locks the resolution
    # that drives them, so the "member cap without a budget is a no-op" reading can't creep back.
    from oraclous_ohm.manifest import OHMMember, resolve_member_caps

    member = OHMMember(role="fact-checker", kind="agent", manifest_ref="org:x/a@1", max_tokens=80)
    assert resolve_member_caps(member, None) == (80, None)
