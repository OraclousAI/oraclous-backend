"""Settings env parsing — the engine's per-member harness dispatch bound has a code-level ceiling.

#1067 part 2: the run's own wall-clock budget lives in the harness's reviewed policy catalogue with
no environment override; the engine's per-member dispatch bound (``harness_member_call_timeout``) is
an ``ENGINE_``-prefixed settings field an operator CAN raise from the environment. Without a
code-level ceiling, raising that environment variable would silently defeat the locked-down run
budget it exists to sit inside of, making the locked one meaningless. The ceiling itself carries no
environment override — only ``harness_member_call_timeout`` does, and it is clamped DOWN to the
ceiling, never up: lowering it is legitimate (a stricter operator), raising it past the ceiling is
not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from oraclous_execution_engine_service.core.config import (
    HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS,
    Settings,
)

pytestmark = pytest.mark.unit


def test_env_above_the_ceiling_is_clamped_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_HARNESS_MEMBER_CALL_TIMEOUT", "9999")
    assert Settings().harness_member_call_timeout == HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS


def test_env_below_the_ceiling_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    below = HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS - 40
    monkeypatch.setenv("ENGINE_HARNESS_MEMBER_CALL_TIMEOUT", str(below))
    assert Settings().harness_member_call_timeout == below


def test_env_exactly_at_the_ceiling_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ENGINE_HARNESS_MEMBER_CALL_TIMEOUT", str(HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS)
    )
    assert Settings().harness_member_call_timeout == HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS


def test_unset_env_defaults_to_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is already at the ceiling, so an unconfigured deploy is never clamped."""
    monkeypatch.delenv("ENGINE_HARNESS_MEMBER_CALL_TIMEOUT", raising=False)
    assert Settings().harness_member_call_timeout == HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS


# ── the ordering across every profile in the harness's own catalogue, pinned so it cannot drift
# back silently (test-quality review, #1071) ───────────────────────────────────────────────────

#: The one accepted, deliberate exception: staging-default's own 300s run budget exceeds the
#: engine's ceiling. The owner ruled it stays at 300 (unlike development-default, lowered to 200)
#: — this set exists so a FUTURE profile added above the ceiling fails this test loudly, while the
#: one already-known, already-ruled exception does not.
_RULED_EXCEPTIONS = frozenset({"policy-set:staging-default@1.0.0"})


def _e2e_citation_caller_patience_seconds() -> int:
    """A real caller's own patience, read off the citation e2e's ACTUAL source (never retyped) —
    mirrors ``test_team_run_service.py``'s helper of the same shape, so both pinnings track the
    same live number rather than two independently hand-typed copies of 270."""
    e2e_path = (
        Path(__file__).resolve().parents[4]
        / "tests"
        / "e2e"
        / "test_agent_write_citation_gateway_e2e.py"
    )
    e2e_source = e2e_path.read_text()
    poll_def_match = re.search(r"def _poll\([\s\S]*?raise AssertionError", e2e_source)
    assert poll_def_match, "could not find the _poll helper in the e2e file's source"
    poll_source = poll_def_match.group(0)
    tries_match = re.search(r"tries:\s*int\s*=\s*(\d+)", poll_source)
    sleep_match = re.search(r"time\.sleep\((\d+)\)", poll_source)
    assert tries_match and sleep_match, "could not read the e2e poll window off _poll's own source"
    return int(tries_match.group(1)) * int(sleep_match.group(1))


def test_the_ceiling_sits_between_every_profiles_run_budget_and_a_callers_patience() -> None:
    """#1067 part 2 (test-quality review): pin the three-way ordering — a run's own wall-clock
    budget, then the engine's per-member ceiling, then a real caller's own patience — across EVERY
    profile in the harness's built-in policy catalogue, so a future edit to either side (a new/
    changed policy tier, or the ceiling itself) cannot silently break it. ``staging-default`` is
    the one already-ruled exception (see ``_RULED_EXCEPTIONS``); a new profile that isn't in that
    set and exceeds the ceiling fails this test, which is the point.
    """
    from oraclous_harness_runtime_service.domain.policy import POLICY_SETS

    caller_patience = _e2e_citation_caller_patience_seconds()
    assert HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS < caller_patience, (
        "the engine's own ceiling must sit strictly under a real caller's patience"
    )
    assert POLICY_SETS, "the harness catalogue is unexpectedly empty — nothing was pinned"
    checked = 0
    for ref, policy_set in POLICY_SETS.items():
        if ref in _RULED_EXCEPTIONS:
            continue
        assert policy_set.max_wall_time_seconds is not None, (
            f"{ref} declares no max_wall_time_seconds — the ordering has nothing to pin against"
        )
        checked += 1
        assert policy_set.max_wall_time_seconds <= HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS, (
            f"{ref}'s own run budget ({policy_set.max_wall_time_seconds}s) exceeds the engine's "
            f"per-member ceiling ({HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS}s) — the engine "
            "would cut the member off before its own governed budget ever gets to fire its "
            "graceful, partial-output-preserving terminal. If this is a newly ruled exception, "
            "add it to _RULED_EXCEPTIONS with a comment naming the ruling; otherwise lower the "
            "profile's budget or raise the ceiling."
        )
    assert checked, "every catalogue entry was exempted — this test would pin nothing"


def test_staging_default_is_the_only_ruled_exception_and_still_exceeds_the_ceiling() -> None:
    """Guards the exception set itself: if staging-default is ever lowered under the ceiling (or
    removed from the catalogue), _RULED_EXCEPTIONS should shrink with it — this fails loudly
    instead of silently keeping a stale exemption around."""
    from oraclous_harness_runtime_service.domain.policy import POLICY_SETS

    for ref in _RULED_EXCEPTIONS:
        assert ref in POLICY_SETS, f"{ref} is listed as a ruled exception but no longer exists"
        budget = POLICY_SETS[ref].max_wall_time_seconds
        assert budget is not None and budget > HARNESS_MEMBER_CALL_TIMEOUT_CEILING_SECONDS, (
            f"{ref} no longer exceeds the ceiling — it is a stale exception; remove it from "
            "_RULED_EXCEPTIONS so the main ordering test covers it again"
        )
