"""#587 — ``on_exhaustion`` flows into the runtime ``PolicyEnvelope`` via ``build_envelope``.

Mirrors the #576 per-member-cap threading: the engine resolves member-over-team and passes the
choice into ``build_envelope``, which sets it on the ``PolicyEnvelope`` the tool-use loop reads at
each breach site. Default ``escalate`` so an envelope built without the param behaves as today.

RED until the [impl] adds ``PolicyEnvelope.on_exhaustion`` + the ``member_on_exhaustion`` param to
``build_envelope``.
"""

from __future__ import annotations

from typing import Any

import pytest
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit


def _ohm() -> Any:
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


def _envelope(**kwargs: Any) -> Any:
    from oraclous_harness_runtime_service.domain.policy import build_envelope, resolve_policy_set

    return build_envelope(_ohm(), resolve_policy_set(None), hard_max_iterations=1000, **kwargs)


def test_envelope_defaults_to_escalate() -> None:
    # no choice → escalate (back-compat: an envelope built the old way behaves exactly as today).
    assert _envelope().on_exhaustion == "escalate"


def test_build_envelope_threads_on_exhaustion() -> None:
    # the resolved member-over-team choice rides into the envelope the loop reads.
    assert _envelope(member_on_exhaustion="degrade").on_exhaustion == "degrade"
    assert _envelope(member_on_exhaustion="escalate").on_exhaustion == "escalate"
