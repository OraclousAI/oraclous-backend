"""Redis client factory (ORAA-4 §21 core layer) — connection setup only, no business logic.

A SYNC ``redis.Redis`` used for the per-(org,graph) community-detect lock (#303). The community
repository is sync (sync Neo4j driver, called via ``asyncio.to_thread``), and the Celery worker is
sync, so the lock client is sync too — a sync ``SET NX EX`` is the natural mutual-exclusion both
paths share. The lock is ADVISORY: a ``None`` client (Redis unconfigured / unreachable) means
detection still runs, just without cross-run mutual exclusion (logged at acquire), exactly like the
retriever's advisory query cache (#308).
"""

from __future__ import annotations

import logging

from oraclous_knowledge_graph_service.core.config import Settings

logger = logging.getLogger(__name__)


def make_redis_lock_client(settings: Settings) -> object | None:
    """Build a sync Redis client for the detect lock, or ``None`` when Redis is unreachable.

    Never raises: a connection that cannot be established degrades to ``None`` (lock disabled) so a
    Redis outage never takes community detection down — it only loses the concurrency guard.
    """
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 — advisory: a Redis fault disables the lock, not detect
        logger.warning("community-detect lock disabled: Redis unavailable (%s)", exc)
        return None
