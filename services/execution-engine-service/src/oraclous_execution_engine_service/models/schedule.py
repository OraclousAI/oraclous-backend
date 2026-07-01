"""EngineSchedule ORM model (models layer).

A durable schedule that fires a harness job. Org-scoped (ADR-006). ``cron`` schedules are fired by
Celery Beat (``fire_due``); ``manual`` schedules fire only via the API. ``last_fired_at`` is the
most recent window already fired — the at-least-once dedupe key (with the ``engine_jobs`` (org,
idempotency_key) unique constraint) so a duplicate beat tick never double-fires.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_execution_engine_service.models.base_model import BaseModel


class EngineSchedule(BaseModel):
    __tablename__ = "engine_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(8), nullable=False)
    cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # the fired OHM: inline (self-contained) OR a registry ref (a registered kind=harness). One set.
    manifest_inline: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    manifest_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # WHAT this schedule fires (#489). ``harness_job`` (default, matching the migration
    # server_default so old rows read clean) fires a durable harness engine_job from the inline/ref
    # manifest; ``adopted_tool_run`` fires a capability-registry instance /execute (``instance_id``
    # + ``input_data``, no manifest).
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="harness_job")
    # the curated/adopted capability-registry instance to execute (adopted_tool_run only)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # the input payload forwarded to the instance /execute (adopted_tool_run only); for a ``team``
    # schedule it carries the team-run spec — ``{"sub_harnesses": …, "gate_decisions": …}``.
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # #601 (team only): the persistent graph workspace the standing team's runs read+write across
    # fires — the binding that makes run N+1 see the state run N wrote (ADR-048 dec. 2 / ADR-040).
    graph_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # #601: per-cadence cost ACCRUAL — RAW tokens summed across the sequence of fires (NOT the
    # run-level pool #585, which resets every run). The accumulator #598's per-period cap reads.
    # BigInteger (int8): a lifetime accumulator on a truly-standing team would overflow int4.
    # #598 reinterprets this as the CURRENT-WINDOW accrual when ``budget_period`` is set (reset to 0
    # at the window boundary); with no period set it stays the #601 lifetime accumulator (no reset).
    recurring_cost_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # #598 (ADR-044 L3 / ADR-048 dec 4b — team only): the schedule-level recurring per-period cap.
    # All NULL => cap OFF (default; old rows + non-budgeted teams take the #585/#601 path byte-for-
    # byte). budget_period ∈ {daily,weekly,monthly}; budget_allowance_tokens is the per-window token
    # ceiling the accrual is checked against; budget_window_start anchors the current window (the
    # boundary math compares now to start+period to detect a roll → reset). String(16) for headroom.
    budget_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    budget_allowance_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # True when L3 paused the fleet (enabled=False BY the budget cap) — distinct from a manual
    # disable, so the boundary re-enable sweep only resumes budget-paused schedules, never a row the
    # user disabled by hand.
    budget_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # #544 (team only): the schedule's most recent SUCCEEDED team-run — the SEED for the NEXT fire,
    # so a standing team's recurring refresh carries forward the prior fire's records (the #602
    # seeded-refresh delta on a cron) instead of a cold rebuild each tick. Distinct from
    # ``last_fired_at`` (a window timestamp): this is a run-id. NULL on a schedule that has never
    # settled a SUCCEEDED fire (the first fire is cold) and on non-team schedules.
    last_settled_team_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
