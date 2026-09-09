"""#993 — ``declared_output_keys`` flows into the runtime ``PolicyEnvelope`` via ``build_envelope``.

Mirrors the #961 ``required_sites`` threading: the engine resolves the member's OWN declared output
keys (``outputs_schema.required``) and passes them through so the tool-use loop can guarantee their
SHAPE on the way out — a declared key holds a string or a list of strings directly, never a nested
object (Contract ruling, owner-approved 2026-09-10). Default ``()`` so an envelope built the old way
is byte-for-byte unchanged.

RED until the [impl] adds ``PolicyEnvelope.declared_output_keys`` + the ``declared_output_keys``
param to ``build_envelope``.
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
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ac",
            "name": "T",
            "owner_organization_id": "01976e3a-0000-7000-9c45-000000000001",
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


def test_envelope_defaults_declared_output_keys_to_empty() -> None:
    # no declaration → () (back-compat: an envelope built the old way behaves exactly as today).
    assert _envelope().declared_output_keys == ()


def test_build_envelope_threads_declared_output_keys() -> None:
    keys = ("linked_summary", "artifact_refs")
    assert _envelope(declared_output_keys=keys).declared_output_keys == keys
