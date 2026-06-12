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

from oraclous_knowledge_graph_service.core.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "oraclous_kgs",
    broker=_settings.celery_broker,
    backend=_settings.celery_backend,
    include=[
        "oraclous_knowledge_graph_service.tasks.ingest_tasks",
        "oraclous_knowledge_graph_service.tasks.community_tasks",
        "oraclous_knowledge_graph_service.tasks.code_stale_tasks",
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

# Stage-6 code stale-symbol sweep (#305): a periodic dispatcher that fans out one per-graph cleanup
# for every code graph. Only runs when an operator deploys a `celery beat` process; a worker-only
# deploy (the current default) ignores this — the sweep is then driven by the post-re-ingest enqueue
# in `ingest_tasks`. Cadence is env-tunable (KGS_CODE_STALE_SWEEP_INTERVAL_SECONDS, default daily).
celery_app.conf.beat_schedule = {
    "code-stale-sweep": {
        "task": "kgs.sweep_all_code_graphs",
        "schedule": float(_settings.code_stale_sweep_interval_seconds),
    },
}


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
