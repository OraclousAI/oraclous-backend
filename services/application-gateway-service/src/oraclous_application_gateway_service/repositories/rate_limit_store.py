"""Rate-limit store (ORAA-4 §21 repositories layer) — the ONLY Redis access in the gateway.

A fixed-window counter (INCR + EXPIRE in one transactional pipeline — a crash between them cannot
leave a TTL-less key that blocks the bucket forever), lifted from the auth-service limiter. The edge
limiter keys by client IP; R7-SEC S3 reuses the same seam with distinct namespaces for the per-key
and per-subscription buckets. ``hit`` RAISES on any Redis fault — the caller owns the fail-open
decision; ``enforce_bucket`` is the shared fail-open wrapper for the per-key/per-sub limits.
"""

from __future__ import annotations

from oraclous_application_gateway_service.domain.edge_protection import RateLimitDecision

_KEY_NS = "rl:edge:ip:"  # the edge-wide per-IP bucket (distinct from auth's rl:agent:/rl:invite:)


class RateLimitStore:
    def __init__(self, redis) -> None:  # noqa: ANN001 — redis.asyncio client (no hard import dep)
        self._redis = redis

    async def hit(
        self, identity: str, *, limit: int, window_seconds: int, namespace: str = _KEY_NS
    ) -> RateLimitDecision:
        """Count one request against ``identity``'s fixed window in ``namespace``; allow while
        count <= limit. Default namespace is the edge per-IP bucket."""
        key = f"{namespace}{identity}"
        async with self._redis.pipeline(transaction=True) as pipe:
            await pipe.incr(key)
            await pipe.expire(key, window_seconds)
            results = await pipe.execute()
        count = int(results[0])
        if count <= limit:
            return RateLimitDecision(allowed=True, retry_after=0)
        ttl = await self._redis.ttl(key)
        retry_after = max(int(ttl) if ttl is not None else 1, 1)
        return RateLimitDecision(allowed=False, retry_after=retry_after)


async def enforce_bucket(
    redis,  # noqa: ANN001 — redis.asyncio client | None
    *,
    identity: str,
    limit: int,
    window_seconds: int,
    namespace: str,
) -> RateLimitDecision:
    """Hit a named bucket, FAIL-OPEN (a missing/erroring Redis allows the request, exactly like the
    edge limiter — never self-DoS on an infra blip). The shared seam for the R7-SEC S3 per-key and
    per-subscription limits."""
    if redis is None:
        return RateLimitDecision(allowed=True, retry_after=0)
    try:
        return await RateLimitStore(redis).hit(
            identity, limit=limit, window_seconds=window_seconds, namespace=namespace
        )
    except Exception:  # noqa: BLE001 — fail-open on any redis error (incl. connect)
        return RateLimitDecision(allowed=True, retry_after=0)
