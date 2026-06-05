"""Domain enums (ORAA-4 §21 models layer)."""

from __future__ import annotations

import enum


class DescriptorKind(enum.StrEnum):
    """The kind of capability a descriptor describes (OHM unified model).

    A *tool* is a concrete executable connector/integration; the other kinds are reserved for the
    harness-runtime (R4) and are accepted by the registry but not executed here.
    """

    TOOL = "tool"
    SKILL = "skill"
    AGENT = "agent"
    HARNESS = "harness"
    HUMAN_ROLE = "human_role"
