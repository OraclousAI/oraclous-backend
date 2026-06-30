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
    TEAM = "team"  # #601: a standing-team team-run bound to a persistent graph workspace


class BudgetPeriod(enum.StrEnum):
    """#598 (ADR-044 L3 / ADR-048 dec 4b): the recurring-budget WINDOW a standing team's per-period
    cap accrues over and resets at. User-chosen; a plain String column (no PG enum) so a future
    value needs no DB migration, matching ``ScheduleType``/``TargetKind``."""

    DAILY = "daily"  # the UTC day [00:00, next 00:00)
    WEEKLY = "weekly"  # the ISO week [Monday 00:00 UTC, next Monday)
    MONTHLY = "monthly"  # the calendar month [day-1 00:00 UTC, next month)


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
