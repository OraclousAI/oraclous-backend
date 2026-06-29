"""Enums shared by the schema (DTO) and models (ORM) layers."""

from __future__ import annotations

import enum


class ScheduleType(enum.StrEnum):
    """WHEN a schedule fires (orthogonal to ``TargetKind``, which controls WHAT it fires)."""

    MANUAL = "manual"  # fired only via the API (no automatic firing)
    CRON = "cron"  # a cron expression, fired by Celery Beat
    EVENT = "event"  # an external event (wiring is a later capability)


class TargetKind(enum.StrEnum):
    """WHAT a schedule fires (orthogonal to ``ScheduleType``, which controls WHEN). Default
    ``harness_job`` so existing/old schedule rows read as the original harness-job path (#489)."""

    HARNESS_JOB = "harness_job"  # the original path: enqueue a durable harness engine_job
    ADOPTED_TOOL_RUN = "adopted_tool_run"  # enqueue a capability-registry instance /execute


class EngineJobState(enum.StrEnum):
    """The durable state of an engine job (the checkpoint state machine around a harness run)."""

    QUEUED = "QUEUED"  # accepted, not yet running
    RUNNING = "RUNNING"  # the worker is executing the harness
    SUCCEEDED = "SUCCEEDED"  # terminal — the harness completed
    PARTIAL = "PARTIAL"  # terminal — completed degraded (#580: proceeded without retrieval data)
    FAILED = "FAILED"  # terminal — the harness failed (after retries)
    ESCALATED = "ESCALATED"  # wait state — paused for a human (resolved by complete/approve/cancel)
    TIMED_OUT = "TIMED_OUT"  # terminal — exceeded the declared wall-clock budget
    CANCELLED = "CANCELLED"  # terminal — cancelled by the caller
