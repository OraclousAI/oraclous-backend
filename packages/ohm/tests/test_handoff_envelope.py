"""The typed hand-off envelope — the inter-member medium (#420; ADR-035 §3).

Replaces the round-table's flattened 4000-char string with a typed payload that is schema-validated
against the producer's ``outputs_schema`` (fail-closed); it carries data only, never capability.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.envelope import HandoffEnvelope, build_handoff, validate_payload
from oraclous_ohm.errors import OHMHandoffError
from oraclous_ohm.manifest import OHMMember


def _m(role: str, outputs_schema: dict | None = None) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", outputs_schema=outputs_schema or {}
    )


def test_envelope_carries_data_not_capability() -> None:
    env = HandoffEnvelope(from_role="a", to_role="b", payload={"x": 1})
    assert env.from_role == "a"
    assert env.to_role == "b"
    assert env.payload == {"x": 1}
    assert not hasattr(env, "tools")  # data only, never capability (ADR-035 §3 isolation)


def test_validate_empty_schema_is_lenient() -> None:
    assert validate_payload({"anything": 1}, {}) == []


def test_validate_required_keys() -> None:
    schema = {"required": ["ledger", "count"]}
    assert validate_payload({"ledger": [], "count": 3}, schema) == []
    errs = validate_payload({"ledger": []}, schema)
    assert len(errs) == 1 and "count" in errs[0]


def test_build_handoff_valid() -> None:
    prod = _m("researcher", {"required": ["ledger"]})
    env = build_handoff(
        prod, _m("analyst"), {"ledger": [1, 2]}, objective_slice="analyze", provenance_ref="run-7"
    )
    assert env.from_role == "researcher"
    assert env.to_role == "analyst"
    assert env.payload == {"ledger": [1, 2]}
    assert env.objective_slice == "analyze"
    assert env.provenance_ref == "run-7"


def test_build_handoff_fails_closed_on_bad_payload() -> None:
    prod = _m("researcher", {"required": ["ledger"]})
    with pytest.raises(OHMHandoffError):
        build_handoff(prod, _m("analyst"), {"wrong": 1})


def test_build_handoff_lenient_when_no_schema() -> None:
    env = build_handoff(_m("a"), _m("b"), {"free": "form"})  # no outputs_schema -> no validation
    assert env.payload == {"free": "form"}
