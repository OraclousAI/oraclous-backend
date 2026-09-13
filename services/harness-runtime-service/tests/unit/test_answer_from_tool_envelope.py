"""#900 — ``answer_from_tool`` flows into the runtime ``PolicyEnvelope`` via ``build_envelope``.

ADR-053 decision 2: the declaration lives on the member descriptor (``OHMMember.answer_from_tool``,
pinned test-only in ``test_answer_from_tool_manifest.py`` — the actual manifest field is not built
yet either), but the tool-use loop itself reads the effective value off the runtime's
``PolicyEnvelope``, exactly as ``requires_valid_json`` already does (`test_requires_valid_json_
envelope.py`) and ``on_exhaustion`` before it (`test_on_exhaustion_envelope.py`). The engine
resolves the member's declaration and passes it into ``build_envelope``, which sets it on the
``PolicyEnvelope`` the loop reads to decide when a tool call IS the member's answer. Default
``None`` so an envelope built without the param behaves as today.

RED until the [impl] adds ``PolicyEnvelope.answer_from_tool`` + the ``member_answer_from_tool``
param to ``build_envelope``. Today neither exists, so the "defaults to None" case is not a False/
None assertion failure — it is an ``AttributeError`` (the field is not on the dataclass at all),
and the "threads a value" case is a ``TypeError`` (``build_envelope`` has no such parameter). Both
are written as plain assertions/calls, not wrapped in ``pytest.raises`` — that would turn a RED
failure into a fabricated pass.
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
        "capabilities": [{"ref": "core/web.search@1.0.0", "binding": "web.search"}],
        "models": [{"role": "primary", "binding": "anthropic/m", "protocol_shape": "native"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "runtime": {"entrypoint": "web.search"},
    }
    return load_ohm(doc)


def _envelope(**kwargs: Any) -> Any:
    from oraclous_harness_runtime_service.domain.policy import build_envelope, resolve_policy_set

    return build_envelope(_ohm(), resolve_policy_set(None), hard_max_iterations=1000, **kwargs)


def test_envelope_defaults_answer_from_tool_to_none() -> None:
    # no declaration → None (back-compat: an envelope built the old way behaves exactly as today).
    assert _envelope().answer_from_tool is None


def test_build_envelope_threads_answer_from_tool() -> None:
    assert _envelope(member_answer_from_tool="web.search").answer_from_tool == "web.search"
    assert _envelope(member_answer_from_tool=None).answer_from_tool is None
