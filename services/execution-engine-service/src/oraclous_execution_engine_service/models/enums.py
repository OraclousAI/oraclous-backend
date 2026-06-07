"""Enums shared by the schema (DTO) and models (ORM) layers."""

from __future__ import annotations

import enum


class EngineJobState(enum.StrEnum):
    """The durable state of an engine job (the checkpoint state machine around a harness run)."""

    QUEUED = "QUEUED"  # accepted, not yet running
    RUNNING = "RUNNING"  # the worker is executing the harness
    SUCCEEDED = "SUCCEEDED"  # terminal — the harness completed
    FAILED = "FAILED"  # terminal — the harness failed (after retries)
    ESCALATED = "ESCALATED"  # wait state — paused for a human (resolved by complete/approve/cancel)
    TIMED_OUT = "TIMED_OUT"  # terminal — exceeded the declared wall-clock budget
    CANCELLED = "CANCELLED"  # terminal — cancelled by the caller
