"""Enums shared by the schema (DTO) and models (ORM) layers."""

from __future__ import annotations

import enum


class HarnessStatus(enum.StrEnum):
    """Terminal outcome of a harness execution."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # the loop hit a gate (budget/HITL/iteration cap) without a final answer — slice 3+.
    ESCALATED = "ESCALATED"
    # #587: a budget gate under on_exhaustion=degrade — the loop FINISHED with its best-effort
    # last_text (a flagged partial, not a crash and not a resumable pause). #580 reuses this.
    PARTIAL = "PARTIAL"


class StepKind(enum.StrEnum):
    """The kind of a single step in the tool-use loop's trace."""

    LLM = "llm"  # one model turn
    TOOL = "tool"  # one capability dispatch
    GATE = "gate"  # a governance decision (budget halt / HITL gate / forbidden)
