"""Per-graph generation counter (ORAA-4 §21 repositories layer — the cache-invalidation seam, #308).

The KGS writes the graph; the knowledge-retriever caches reads. After a successful ingest the KGS
``INCR``s a neutral per-graph "generation" counter in Redis (``graph_generation_key`` — a graph-
version signal, NOT the retriever's private cache-key layout); the retriever folds the current
generation into its cache key, so a fresh ingest makes every prior entry a natural cache-miss with
no cross-service key-format coupling. The KGS therefore never touches a retriever cache key — it
only bumps a version number both sides agree on through the shared substrate convention.

Advisory: a Redis outage means the bump is skipped (the cache then expires by TTL instead) — it
must never fail the ingest, so every Redis error is swallowed.
"""

from __future__ import annotations

import logging

from oraclous_substrate.cache_keys import graph_generation_key

logger = logging.getLogger(__name__)


class GraphGenerationRepository:
    """Bumps the per-graph generation counter that the retriever's cache key folds in."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    @classmethod
    def bump_for(cls, *, redis_url: str, organisation_id: str, graph_id: str) -> None:
        """Open a short-lived sync Redis client from ``redis_url``, bump, close (#308).

        The Redis driver is constructed HERE (the repositories layer, ORAA-4 §21) — never in the
        Celery task — so a worker stays free of direct driver imports. Task-scoped: a worker has no
        shared client, so the connection is opened and closed per ingest, mirroring the per-task
        Neo4j-driver / NullPool-engine discipline (ADR-012). Fully advisory (see ``bump``).
        """
        import redis as redis_lib

        client = redis_lib.from_url(redis_url, decode_responses=True)
        try:
            cls(client).bump(organisation_id=organisation_id, graph_id=graph_id)
        finally:
            client.close()

    def bump(self, *, organisation_id: str, graph_id: str) -> None:
        """INCR the per-graph generation; a fresh generation is a natural retriever cache-miss.

        Swallows any Redis error — invalidation is advisory and must never fail the ingest (the
        retriever cache then falls back to TTL expiry).
        """
        try:
            self._redis.incr(graph_generation_key(organisation_id, graph_id))
        except Exception as exc:  # noqa: BLE001 — advisory: a failed bump falls back to TTL expiry
            logger.warning(
                "graph generation bump skipped (graph_id=%s): %s — retriever cache will TTL-expire",
                graph_id,
                exc,
            )
