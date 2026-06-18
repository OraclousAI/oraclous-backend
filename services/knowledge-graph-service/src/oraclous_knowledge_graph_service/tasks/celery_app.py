"""Celery app + sync→async bridge (R3.5-P1-S2).

One Celery app, redis broker + result backend (config block lifted from legacy develop@84152635).
`AsyncTaskExecutor` runs the async ingestion impl on a FRESH event loop per task — the canonical
sync-Celery-worker → async-work pattern; never reuse a long-lived loop, never `asyncio.run` inside a
worker. ContextVars (the org scope) propagate across `await` within the one loop.
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

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_worker_engine
from oraclous_knowledge_graph_service.core.rls import (
    RlsBypassingRoleError,
    assert_runtime_role_isolates,
)

_settings = get_settings()

celery_app = Celery(
    "oraclous_kgs",
    broker=_settings.celery_broker,
    backend=_settings.celery_backend,
    include=[
        "oraclous_knowledge_graph_service.tasks.ingest_tasks",
        "oraclous_knowledge_graph_service.tasks.community_tasks",
        "oraclous_knowledge_graph_service.tasks.code_stale_tasks",
        "oraclous_knowledge_graph_service.tasks.memory_tasks",
    ],
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
)

# Periodic dispatchers. Both only run when an operator deploys a `celery beat` process; a worker-
# only deploy (the current default) ignores them.
#   * code-stale-sweep (#305): fans out one per-graph stale-symbol cleanup for every code graph
#     (otherwise driven by the post-re-ingest enqueue in `ingest_tasks`).
#   * memory-consolidation-sweep (#332): fans out one BOUNDED per-(org,graph) consolidation for
#     every graph owning current memories, so the similarity consolidation actually runs on a
#     cadence rather than only on an explicit POST .../memories/consolidate.
# Cadences are env-tunable (KGS_CODE_STALE_SWEEP_INTERVAL_SECONDS /
# KGS_MEMORY_CONSOLIDATION_SWEEP_INTERVAL_SECONDS, both default daily).
celery_app.conf.beat_schedule = {
    "code-stale-sweep": {
        "task": "kgs.sweep_all_code_graphs",
        "schedule": float(_settings.code_stale_sweep_interval_seconds),
    },
    "memory-consolidation-sweep": {
        "task": "kgs.consolidate_all_memory_graphs",
        "schedule": float(_settings.memory_consolidation_sweep_interval_seconds),
    },
}


# --- #366 part 2: OTel tracing + WP-6 request-id threading across the broker ----------------------
# Tracing init is GATED (no-op unless OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_ENABLED is set) and runs in
# the WORKER process only (worker_process_init) — never on the web side that merely imports this
# module to call `.delay()`, so the CeleryInstrumentor is installed lazily and only where a worker
# actually runs. The three task signals carry the WP-6 request_id across the broker: attach it to
# the message headers at publish (still in the request's bound context), re-bind it in the worker at
# task start, reset it at task end — so worker logs + spans share the web-path id.

#: reset-token store keyed by Celery task_id, set in `task_prerun` + consumed in `task_postrun`.
#: A module dict (not a task attribute) so the bind/reset pair is robust across task types/retries.
_request_id_tokens: dict[str, object] = {}


@worker_process_init.connect
def _configure_worker_tracing(**_kwargs: Any) -> None:
    """Install OTel tracing for this worker process (gated no-op when OTel is unconfigured).

    Fires once per prefork child at boot — the worker mirror of the factory's ``instrument_app``.
    KGS uses neo4j, but the contrib project ships no neo4j-driver instrumentor (see
    packages/telemetry/pyproject.toml), so ``with_neo4j`` simply attempts it best-effort and skips.
    """
    instrument_worker("knowledge-graph-service")


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
    backstop that it runs under a NOSUPERUSER/NOBYPASSRLS role; a worker mis-deployed on the owner
    DSN would silently bypass RLS — T1-M3).

    Fires on ``worker_process_init`` — ONCE per worker process (each prefork child at boot), NOT
    per task. Gated on ``rls_assert_runtime_role`` (default false), so a dev/test run on the owner
    DSN starts the worker normally. When on, it builds one throwaway worker engine, asserts the
    runtime role cannot bypass RLS, and on a bypassing role fails closed LOUDLY (log +
    ``SystemExit``) so the worker process refuses to come up under an inert backstop. The engine is
    disposed either way.
    """
    settings = get_settings()
    if not settings.rls_assert_runtime_role:
        return

    async def _check() -> None:
        engine = make_worker_engine()
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
            "knowledge-graph-service",
            "worker runtime DB role bypasses RLS; refusing to start (ADR-030 §3)",
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
