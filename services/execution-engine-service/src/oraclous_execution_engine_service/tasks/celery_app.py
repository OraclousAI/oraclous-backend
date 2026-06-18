"""Celery app + sync→async bridge (R5-S2).

One Celery app, Redis broker + result backend. ``AsyncTaskExecutor`` runs the async job impl on a
FRESH event loop per task — the canonical sync-Celery-worker → async-work pattern (never reuse a
long-lived loop, never ``asyncio.run`` inside a worker). ContextVars (the org scope) propagate over
``await`` within the one loop. (Pattern lifted from knowledge-graph-service.)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from celery import Celery
from celery.signals import before_task_publish, task_postrun, task_prerun, worker_process_init
from oraclous_telemetry import (
    Severity,
    alert,
    attach_request_id,
    bind_request_id_from_headers,
    clear_request_id,
    instrument_worker,
)

from oraclous_execution_engine_service.core.config import get_settings
from oraclous_execution_engine_service.core.rls import (
    RlsBypassingRoleError,
    assert_runtime_role_isolates,
    build_rls_engine,
)

_settings = get_settings()

celery_app = Celery(
    "oraclous_engine",
    broker=_settings.celery_broker,
    backend=_settings.celery_backend,
    include=["oraclous_execution_engine_service.tasks.run_tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=60 * 60,
    task_soft_time_limit=50 * 60,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    # Ack AFTER the task finishes: if a worker dies before it commits QUEUED→RUNNING, the message is
    # redelivered — and the QUEUED→RUNNING CAS makes the re-run idempotent (a no-op if already past
    # QUEUED). A worker dying AFTER RUNNING leaves the job RUNNING (reaped by the S3 lease sweep).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Celery Beat (S5): fire due cron schedules each tick + sweep stranded RUNNING jobs. A SINGLE
    # beat process must run (compose `execution-engine-beat`); firing is idempotent (the engine_jobs
    # (org, idempotency_key) unique constraint), so a double-tick never double-fires.
    beat_schedule={
        "fire-due-schedules": {
            "task": "engine.fire_schedules",
            "schedule": _settings.schedule_tick_seconds,
        },
        "reap-stale-running": {
            "task": "engine.reap_stale",
            "schedule": _settings.reaper_tick_seconds,
        },
    },
)


# --- #366 part 2: OTel tracing + WP-6 request-id threading across the broker ----------------------
# Tracing init is GATED (no-op unless OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_ENABLED is set) and runs in
# the WORKER/beat process only (worker_process_init) — never on the web side that merely imports
# this module to call `.delay()`. The three task signals carry the WP-6 request_id across the
# broker: attach it to the message headers at publish (still in the request's bound context),
# re-bind it in the worker at task start, reset it at task end — so worker logs/spans share the id.

#: reset-token store keyed by Celery task_id, set in `task_prerun` + consumed in `task_postrun`.
#: A module dict (not a task attribute) so the bind/reset pair is robust across task types/retries.
_request_id_tokens: dict[str, object] = {}


@worker_process_init.connect
def _configure_worker_tracing(**_kwargs: Any) -> None:
    """Install OTel tracing for this worker process (gated no-op when OTel is unconfigured).

    Fires once per prefork child at boot — the worker mirror of the factory's ``instrument_app``.
    The engine touches no neo4j, so ``with_neo4j=False``.
    """
    instrument_worker("execution-engine-service", with_neo4j=False)


@before_task_publish.connect
def _attach_request_id(headers: Any = None, **_kwargs: Any) -> None:
    """Copy the request-bound WP-6 ``x-request-id`` onto the outbound Celery message headers.

    Fires on the PUBLISH side — still inside the enqueuing request's bound context (a `.delay()`
    called from a route handler). A no-op when no id is bound (e.g. a Beat-scheduled task), so it
    never breaks publishing.
    """
    attach_request_id(headers)


@task_prerun.connect
def _bind_request_id(task_id: str | None = None, task: Any = None, **_kwargs: Any) -> None:
    """Re-bind the request-id carried in the task headers to this worker's context, at task start.

    Reads the id from the running task's request headers and binds it so every log line + span the
    task emits carries the web-path correlation id. Stashes the reset token under ``task_id`` for
    ``_clear_request_id`` to release.
    """
    headers = getattr(getattr(task, "request", None), "headers", None)
    token = bind_request_id_from_headers(headers)
    if token is not None and task_id is not None:
        _request_id_tokens[task_id] = token


@task_postrun.connect
def _clear_request_id(task_id: str | None = None, **_kwargs: Any) -> None:
    """Reset the request-id bound at task start so a pooled worker never leaks it onward."""
    if task_id is not None:
        clear_request_id(_request_id_tokens.pop(task_id, None))


@worker_process_init.connect
def _assert_runtime_role_once_per_worker(**_kwargs: Any) -> None:
    """ADR-030 §3 fail-closed role assertion for the Celery worker — the worker mirror of the web
    lifespan check (the worker process never runs the FastAPI lifespan, so without this it had no
    backstop that the ORG-BOUND engine runs under a NOSUPERUSER/NOBYPASSRLS role; a worker
    mis-deployed with its org-bound DSN on the owner role would silently bypass RLS — T1-M3).

    Fires on ``worker_process_init`` — ONCE per worker process (each prefork child at boot), NOT per
    task. Gated on ``rls_assert_runtime_role`` (default false), so a dev/test run on the owner DSN
    starts the worker normally. When on, it builds one throwaway ORG-BOUND engine
    (``build_rls_engine(settings.database_url)`` — the same role the job/round-table execution +
    the per-row sweep settle use), asserts that role cannot bypass RLS, and on a bypassing role
    fails closed LOUDLY (log + ``SystemExit``) so the worker refuses to come up under an inert
    backstop. The MAINTENANCE engine (the owner, ``maintenance_url``) is INTENDED to bypass RLS for
    the cross-org sweep reads, so it is deliberately NOT asserted here. The engine is disposed
    either way."""
    settings = get_settings()
    if not settings.rls_assert_runtime_role:
        return

    async def _check() -> None:
        engine = build_rls_engine(settings.database_url)
        try:
            await assert_runtime_role_isolates(engine)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_check())
    except RlsBypassingRoleError as exc:
        alert(
            Severity.ERROR,
            "rls_runtime_role_bypasses",
            "execution-engine-service",
            "worker org-bound DB role bypasses RLS; refusing to start (ADR-030 §3)",
            error=str(exc),
        )
        raise SystemExit(1) from exc


class AsyncTaskExecutor:
    """Bridge a sync Celery task to an async coroutine on a fresh, isolated event loop."""

    @staticmethod
    def run_async_task(async_func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(async_func(*args, **kwargs))
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                loop.close()
                asyncio.set_event_loop(None)
