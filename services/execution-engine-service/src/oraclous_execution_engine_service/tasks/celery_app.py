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

from oraclous_execution_engine_service.core.config import get_settings

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
)


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
