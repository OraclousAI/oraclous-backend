"""The human-gate decision value object (ADR-046 / #578): GateDecision + gate_verb back-compat."""

from __future__ import annotations

import pytest
from oraclous_ohm.gate import GateDecision, gate_verb
from pydantic import ValidationError


def test_bare_string_normalizes_to_a_gate_decision() -> None:
    # v1 back-compat: a bare "approve"/"reject" (or the new "revise") string IS the decision.
    for verb in ("approve", "revise", "reject"):
        gd = GateDecision.model_validate(verb)
        assert gd.decision == verb
        assert gd.feedback == "" and gd.edited_payload is None


def test_full_object_carries_feedback_and_edited_payload() -> None:
    gd = GateDecision.model_validate(
        {"decision": "revise", "feedback": "use the bible's voice", "edited_payload": {"x": 1}}
    )
    assert gd.decision == "revise"
    assert gd.feedback == "use the bible's voice"
    assert gd.edited_payload == {"x": 1}


def test_unknown_verb_and_extra_field_are_rejected() -> None:
    with pytest.raises(ValidationError):
        GateDecision.model_validate({"decision": "maybe"})  # not in the three-verb set
    with pytest.raises(ValidationError):
        GateDecision.model_validate({"decision": "approve", "surprise": 1})  # extra="forbid"


def test_gate_verb_normalizes_every_shape() -> None:
    assert gate_verb(None) is None  # undecided → still paused
    assert gate_verb("approve") == "approve"  # v1 bare string
    assert gate_verb({"decision": "revise", "feedback": "x"}) == "revise"  # persisted JSONB dict
    assert gate_verb(GateDecision(decision="reject")) == "reject"  # the model itself


def test_gate_verb_fails_closed_on_an_unrecognised_shape() -> None:
    # a malformed value reads as undecided (None), never silently crossing the gate.
    assert gate_verb(42) is None
    assert gate_verb({"no_decision_key": True}) is None
    assert gate_verb({"decision": 123}) is None  # non-string decision
