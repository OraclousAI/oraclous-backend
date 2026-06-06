"""Enums shared by the schema (DTO) and models (ORM) layers."""

from __future__ import annotations

import enum


class HarnessStatus(enum.StrEnum):
    """Terminal outcome of a harness execution."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # the loop hit a gate (budget/HITL/iteration cap) without a final answer — slice 3+.
    ESCALATED = "ESCALATED"


class StepKind(enum.StrEnum):
    """The kind of a single step in the tool-use loop's trace."""

    LLM = "llm"  # one model turn
    TOOL = "tool"  # one capability dispatch
    GATE = "gate"  # a governance decision (budget halt / HITL gate / forbidden)
