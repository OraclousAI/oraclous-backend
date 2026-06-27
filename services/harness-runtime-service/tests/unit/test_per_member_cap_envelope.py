"""#576 — the per-member cap flows into the runtime envelope + an OPTIONAL server ceiling.

``build_envelope`` is where the per-member SAFETY CAP overrides the hardcoded policy tier. The core
bug: the development-default tier (200k) is the per-member cap and a user cannot raise it, so heavy
agents die on Oraclous's own cap. With a per-member override the user picks higher; the policy tier
is only the fallback when no override is given (back-compat). The deployment keeps an OPTIONAL
ceiling (default OFF — the user owns the budget; the ceiling is a safety backstop, not a default
tier), distinct from the old unraisable ``force_policy_set`` tier.

RED until the [impl] adds the ``member_max_tokens`` / ``member_max_tool_calls`` /
``max_tokens_ceiling`` / ``max_tool_calls_ceiling`` params to ``build_envelope``.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_DEV_TIER_TOKENS = 200_000  # policy-set:development-default token cap (the unraisable tier today)


def _ohm():
    doc = {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "T",
            "owner_organization_id": "01976e3a-0000-7000-9c45-000000000000",
        },
        "capabilities": [{"ref": "core/postgresql-reader@1.0.0", "binding": "pg"}],
        "models": [{"role": "primary", "binding": "anthropic/m", "protocol_shape": "native"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "runtime": {"entrypoint": "pg"},
    }
    return load_ohm(doc)


def _envelope(**kwargs):
    from oraclous_harness_runtime_service.domain.policy import build_envelope, resolve_policy_set

    return build_envelope(_ohm(), resolve_policy_set(None), hard_max_iterations=1000, **kwargs)


def test_no_override_keeps_the_policy_tier_back_compat() -> None:
    env = _envelope()
    assert env.max_tokens == _DEV_TIER_TOKENS  # unchanged behaviour when the user sets nothing


def test_member_override_replaces_the_policy_tier() -> None:
    env = _envelope(member_max_tokens=80_000)  # a user choosing LOWER than the tier still wins
    assert env.max_tokens == 80_000


def test_member_can_raise_above_the_policy_tier() -> None:
    # THE bug #576 fixes: a user raises a heavy member ABOVE the 200k tier (their key pays).
    env = _envelope(member_max_tokens=750_000)
    assert env.max_tokens == 750_000
    assert env.max_tokens > _DEV_TIER_TOKENS


def test_optional_ceiling_clamps_the_override_when_set() -> None:
    env = _envelope(member_max_tokens=900_000, max_tokens_ceiling=600_000)
    assert env.max_tokens == 600_000  # deployment safety backstop bites


def test_ceiling_is_off_by_default_user_owns_the_budget() -> None:
    # No ceiling passed → a large override stands unclamped. "Picking from 50k/100k/200k isn't
    # ownership" — with the ceiling OFF the user's value is authoritative.
    env = _envelope(member_max_tokens=5_000_000)
    assert env.max_tokens == 5_000_000


def test_member_max_tool_calls_drives_iterations_and_cap() -> None:
    env = _envelope(member_max_tool_calls=300)
    assert env.max_tool_calls == 300
    assert env.max_iterations == 301  # min(hard_max=1000, 300 + 1)


def test_tool_calls_ceiling_clamps_iterations() -> None:
    env = _envelope(member_max_tool_calls=500, max_tool_calls_ceiling=120)
    assert env.max_tool_calls == 120
    assert env.max_iterations == 121
