"""The human-gate decision value object (ADR-046 / #578).

A ``kind: human`` member is a BLOCKING gate (ADR-035 §6). Its decision was a bare
``Literal["approve", "reject"]`` string; ADR-046 promotes it to a **three-verb** typed object so a
human can also **``revise``** — send the rejected producer's sub-tree back to re-run with feedback,
then re-pause at the SAME gate for a fresh decision (bounded, never a fork of the run):

- ``approve`` — accept the output; resume past the gate (unchanged).
- ``revise``  — the output is wrong; re-run the gate's invalidated producer sub-tree with
  ``feedback`` threaded in (or seed ``edited_payload`` verbatim), then re-pause at the same gate.
- ``reject``  — kill the run definitively → terminal ``REJECTED`` (unchanged; a deliberate,
  separate choice, not ``revise``).

BACKWARD-COMPATIBLE: a bare ``"approve"``/``"reject"`` string normalizes to
``GateDecision(decision=…)`` (a ``mode="before"`` validator), so every v1 client, persisted
``gate_decisions`` row, and test keeps working — the widening is additive, never breaking.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

GateVerb = Literal["approve", "revise", "reject"]


class GateDecision(BaseModel):
    """A human's decision on one gate. ``feedback`` (revise) is a prose instruction threaded to the
    producer; ``edited_payload`` (revise) is a verbatim override seeded as the producer's result
    instead of re-running it. Both are ignored for approve/reject."""

    model_config = ConfigDict(extra="forbid")

    decision: GateVerb
    feedback: str = ""
    edited_payload: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_bare_string(cls, value: Any) -> Any:  # noqa: ANN401
        # back-compat: a v1 bare "approve"/"reject" (or a future "revise") string IS the decision.
        if isinstance(value, str):
            return {"decision": value}
        return value


def gate_verb(value: Any) -> str | None:  # noqa: ANN401
    """The decision verb from a gate value that may be a bare string (v1: ``approve``/``reject``),
    a ``GateDecision``-shaped mapping (persisted JSONB: ``{"decision": …, "feedback": …}``), or a
    ``GateDecision`` — ``None`` when the gate is undecided (still PAUSED). Fail-closed: an
    unrecognised shape reads as ``None`` (undecided) rather than silently crossing the gate."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, GateDecision):
        return value.decision
    if isinstance(value, Mapping):
        verb = value.get("decision")
        return verb if isinstance(verb, str) else None
    return None
