"""#587 — ``on_exhaustion: escalate|degrade`` field + member-over-team resolution (back-compat).

A user chooses what happens when a budget/cap is exhausted: ``escalate`` (today — pause/fail for a
human/retry) or ``degrade`` (finish with what's produced, a flagged partial). The field lives on
``OHMMember`` (an optional override, default ``None``) and ``OHMBudget`` (the team default =
``escalate``, so EVERY pre-#587 manifest behaves exactly as today). ``resolve_member_on_exhaustion``
resolves member-over-team-over-hard-default — a SEPARATE resolver, so ``resolve_member_caps`` keeps
its 2-tuple shape and callers don't break.

RED until the [impl] adds the two fields + ``resolve_member_on_exhaustion``.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.manifest import OHMBudget, OHMMember

pytestmark = pytest.mark.unit


def _member(on_exhaustion: str | None = None) -> OHMMember:
    kw: dict = {"role": "a", "kind": "agent", "manifest_ref": "org:x/a@1"}
    if on_exhaustion is not None:
        kw["on_exhaustion"] = on_exhaustion
    return OHMMember(**kw)


def test_member_on_exhaustion_defaults_none() -> None:
    # no member opinion → inherit the team/hard default (back-compat: existing manifests unchanged).
    assert _member().on_exhaustion is None


def test_budget_on_exhaustion_defaults_escalate() -> None:
    # the team default is escalate, so a pre-#587 manifest (no on_exhaustion) escalates as today.
    assert OHMBudget().on_exhaustion == "escalate"


def test_resolve_member_over_team() -> None:
    from oraclous_ohm.manifest import resolve_member_on_exhaustion

    # the member's explicit choice wins over the team default.
    assert (
        resolve_member_on_exhaustion(_member("degrade"), OHMBudget(on_exhaustion="escalate"))
        == "degrade"
    )


def test_resolve_team_default_when_member_unset() -> None:
    from oraclous_ohm.manifest import resolve_member_on_exhaustion

    # member has no opinion → the team default binds.
    assert resolve_member_on_exhaustion(_member(), OHMBudget(on_exhaustion="degrade")) == "degrade"


def test_resolve_hard_default_no_budget() -> None:
    from oraclous_ohm.manifest import resolve_member_on_exhaustion

    # neither a member override nor a team budget → the hard default escalate (back-compat).
    assert resolve_member_on_exhaustion(_member(), None) == "escalate"


def test_resolve_member_caps_shape_unchanged() -> None:
    from oraclous_ohm.manifest import resolve_member_caps

    # on_exhaustion is a SEPARATE resolver — resolve_member_caps still returns its 2-tuple (no
    # caller break); the #576 cap resolution is untouched.
    result = resolve_member_caps(_member(), OHMBudget())
    assert isinstance(result, tuple) and len(result) == 2
