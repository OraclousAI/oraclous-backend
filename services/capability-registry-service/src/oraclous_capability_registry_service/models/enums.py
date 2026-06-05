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


class InstanceStatus(enum.StrEnum):
    """Lifecycle of a configured tool instance.

    ``PENDING`` just created; ``CONFIGURATION_REQUIRED`` missing credential mappings;
    ``READY`` all required credentials mapped and executable; the remaining states are set by the
    execution engine (S4+).
    """

    PENDING = "PENDING"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
