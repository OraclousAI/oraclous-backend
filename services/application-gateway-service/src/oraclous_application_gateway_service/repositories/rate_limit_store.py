"""Edge rate-limit store (ORAA-4 §21 repositories layer) — the ONLY Redis access in the gateway.

A fixed-window counter (INCR + EXPIRE in one transactional pipeline — a crash between them cannot
leave a TTL-less key that blocks the bucket forever), lifted from the auth-service limiter and
reshaped to key by client IP for an edge-wide limit. It RAISES on any Redis fault — the caller (the
rate-limit middleware) owns the fail-open decision, so this layer stays a pure I/O seam.
"""

from __future__ import annotations

from oraclous_application_gateway_service.domain.edge_protection import RateLimitDecision

_KEY_NS = "rl:edge:ip:"  # distinct from auth's rl:agent:pfx:/rl:invite:pfx: namespaces


class RateLimitStore:
    def __init__(self, redis) -> None:  # noqa: ANN001 — redis.asyncio client (no hard import dep)
        self._redis = redis

    async def hit(self, client_ip: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        """Count one request against the IP's fixed window; allow while count <= limit."""
        key = f"{_KEY_NS}{client_ip}"
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
