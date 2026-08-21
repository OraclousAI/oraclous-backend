"""#853 — ``requires_valid_json`` flows into the runtime ``PolicyEnvelope`` via ``build_envelope``.

Mirrors the #587 ``on_exhaustion`` threading (`test_on_exhaustion_envelope.py`): the engine resolves
the member's declaration and passes it into ``build_envelope``, which sets it on the
``PolicyEnvelope`` the tool-use loop reads to decide whether a malformed ``graph-ingest`` document
earns one repair turn. Default ``False`` so an envelope built without the param behaves as today.

RED until the [impl] adds ``PolicyEnvelope.requires_valid_json`` + the
``member_requires_valid_json`` param to ``build_envelope``.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _ohm() -> Any:
    from oraclous_ohm.parse import load_ohm

    doc = {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "T",
            "owner_organization_id": "01976e3a-0000-7000-9c45-000000000000",
        },
        "capabilities": [{"ref": "core/graph-ingest@1.0.0", "binding": "graph-ingest"}],
        "models": [{"role": "primary", "binding": "anthropic/m", "protocol_shape": "native"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "runtime": {"entrypoint": "graph-ingest"},
    }
    return load_ohm(doc)


def _envelope(**kwargs: Any) -> Any:
    from oraclous_harness_runtime_service.domain.policy import build_envelope, resolve_policy_set

    return build_envelope(_ohm(), resolve_policy_set(None), hard_max_iterations=1000, **kwargs)


def test_envelope_defaults_requires_valid_json_to_false() -> None:
    # no declaration → False (back-compat: an envelope built the old way behaves exactly as today).
    assert _envelope().requires_valid_json is False


def test_build_envelope_threads_requires_valid_json() -> None:
    assert _envelope(member_requires_valid_json=True).requires_valid_json is True
    assert _envelope(member_requires_valid_json=False).requires_valid_json is False
