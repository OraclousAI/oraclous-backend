"""Redis query cache (repositories layer — the only Redis access, #308).

Lift-and-reshape of the legacy ``knowledge-graph-builder/app/services/query_cache_service.py``:
cache the search/subgraph read envelope keyed by ``org + graph + generation + query-hash`` (the
key convention lives once in ``oraclous_substrate.cache_keys`` so neither service knows the other's
key layout). The retriever is read-only, so it never writes a graph; invalidation is handled by the
*generation counter* the KGS bumps on ingest — this repository only READS that counter and folds it
into the cache key, so a fresh ingest is a natural cache-miss. No SCAN-and-delete, no key-format
coupling across the KGS↔retriever boundary.

Advisory by contract: a ``None`` client (cache disabled / no Redis) makes every operation a silent
no-op, and any Redis error degrades to a live query — the request path is never blocked or failed by
the cache. Async client (``redis.asyncio``) for the FastAPI read path.
"""

from __future__ import annotations

import json
import logging

from oraclous_substrate.cache_keys import graph_generation_key, query_cache_key

logger = logging.getLogger(__name__)


class QueryCacheRepository:
    """Advisory Redis cache for retrieval read results, scoped per org+graph+generation."""

    def __init__(self, redis_client, *, organisation_id: str, ttl: int = 300) -> None:
        """Initialise the cache layer.

        Args:
            redis_client: an ``redis.asyncio.Redis`` (or compatible) instance, or ``None`` to run
                in a no-op / cache-disabled mode (every operation becomes a silent no-op).
            organisation_id: the bound tenant scope — the outermost cache-key segment.
            ttl: seconds a cached entry survives absent a generation bump (a bounded staleness
                backstop on top of generation invalidation).
        """
        self._redis = redis_client
        self._org = organisation_id
        self._ttl = ttl

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    async def _generation(self, graph_id: str) -> int:
        """Current per-graph generation (0 if unset or Redis is unavailable).

        The KGS ``INCR``s this neutral counter on each successful ingest; reading it here and
        folding it into the cache key is what makes a fresh ingest a cache-miss without the KGS
        ever touching a retriever cache key.
        """
        if self._redis is None:
            return 0
        try:
            raw = await self._redis.get(graph_generation_key(self._org, graph_id))
            return int(raw) if raw is not None else 0
        except Exception as exc:  # noqa: BLE001 — advisory: an unreadable generation -> gen 0
            logger.warning("QueryCache: generation read failed (%s) — using generation 0", exc)
            return 0

    async def get(self, *, graph_id: str, query_text: str, retriever_type: str) -> dict | None:
        """Return the cached envelope, or ``None`` on miss / disabled / Redis unavailable.

        Never raises: any Redis error returns ``None`` so the caller falls through to a live query.
        """
        if self._redis is None:
            return None
        try:
            generation = await self._generation(graph_id)
            key = query_cache_key(
                self._org, graph_id, query_text, retriever_type, generation=generation
            )
            raw = await self._redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as exc:  # noqa: BLE001 — advisory: any cache error is a miss
            logger.warning("QueryCache.get: degraded to live query (%s)", exc)
            return None

    async def set(
        self, *, graph_id: str, query_text: str, retriever_type: str, result: dict
    ) -> None:
        """Cache ``result`` under the current generation for the configured TTL.

        Silently ignores any Redis error — a cache write failure must never block the response.
        """
        if self._redis is None:
            return
        try:
            generation = await self._generation(graph_id)
            key = query_cache_key(
                self._org, graph_id, query_text, retriever_type, generation=generation
            )
            await self._redis.set(key, json.dumps(result, default=str), ex=self._ttl)
        except Exception as exc:  # noqa: BLE001 — advisory: a write failure is silently dropped
            logger.warning("QueryCache.set: result not cached (%s)", exc)
