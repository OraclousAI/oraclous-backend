"""ADR-043 #554 (slice 3/3) — the ``consciousness.permissions`` OHM governance field. Flow-6 Learn's
permission posture: record / suggest / propose / never_auto_apply. The LOAD-BEARING invariant is
``never_auto_apply`` — no harness applies a learned behaviour change without human review.
Additive + runtime-enforced (consulted at execute-time like ``policy_set_ref``); NOT a Contract.

RED until OHMGovernance grows the field — imported function-locally so the module collects.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _gov(**kw):
    from oraclous_ohm.manifest import OHMGovernance

    return OHMGovernance(**kw)


def test_consciousness_permissions_accepts_the_closed_set() -> None:
    for value in ("record", "suggest", "propose", "never_auto_apply"):
        assert _gov(consciousness_permissions=value).consciousness_permissions == value


def test_consciousness_permissions_defaults_none() -> None:
    # absent → None (a harness with no declared posture; the runtime treats it as no consciousness)
    assert _gov().consciousness_permissions is None


def test_consciousness_permissions_rejects_an_unknown_value() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _gov(consciousness_permissions="auto_apply")  # the dangerous value must NOT be expressible
