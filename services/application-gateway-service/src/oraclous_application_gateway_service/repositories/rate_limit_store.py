"""Rate-limit store (repositories layer) — the ONLY Redis access in the gateway.

A fixed-window counter (INCR + EXPIRE in one transactional pipeline — a crash between them cannot
leave a TTL-less key that blocks the bucket forever), lifted from the auth-service limiter. The edge
limiter keys by client IP; R7-SEC S3 reuses the same seam with distinct namespaces for the per-key
and per-subscription buckets. ``hit`` RAISES on any Redis fault — the caller owns the fail-open
decision; ``enforce_bucket`` is the shared fail-open wrapper for the per-key/per-sub limits.

On a Redis OUTAGE the limiter's behaviour is policy (ADR-021 §1): the default is fail-OPEN (allow,
so a transient blip can't self-DoS the sole ingress), but every fail-open emits a structured alert
(``rate_limiter_fail_open``) — the silent swallow is gone. A hardened deploy may opt in to
fail-CLOSED (``allow_during_outage=False``): the outage then raises ``RateLimiterUnavailable``,
which the caller maps to a 503.
"""

from __future__ import annotations

from oraclous_telemetry import Severity, alert

from oraclous_application_gateway_service.domain.edge_protection import RateLimitDecision

_KEY_NS = "rl:edge:ip:"  # the edge-wide per-IP bucket (distinct from auth's rl:agent:/rl:invite:)
_SERVICE = "application-gateway-service"


class RateLimiterUnavailable(Exception):
    """Raised when Redis is unavailable AND the deploy opted into fail-CLOSED
    (``allow_during_outage=False``). The caller maps it to a 503 — the request is refused rather
    than silently un-limited. Never raised in the default (fail-open) configuration."""


def _alert_fail_open(*, namespace: str, reason: str, fail_closed: bool) -> None:
    """Emit the structured visibility signal for a rate-limiter Redis outage (ADR-021 §1).

    Severity escalates with the policy: a fail-OPEN bypass is a WARNING (degraded but still
    serving); a fail-CLOSED refusal is an ERROR (the edge is now 503-ing traffic, the operator must
    act). The stable ``code`` lets ops dashboard/alert on the bypass regardless of severity.
    """
    alert(
        Severity.ERROR if fail_closed else Severity.WARNING,
        "rate_limiter_fail_open",
        _SERVICE,
        (
            "rate limiter could not reach Redis; "
            + (
                "REFUSING the request (fail-closed, 503)"
                if fail_closed
                else "ALLOWING it (fail-open)"
            )
        ),
        namespace=namespace,
        reason=reason,
        fail_closed=fail_closed,
    )


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
    allow_during_outage: bool = True,
) -> RateLimitDecision:
    """Hit a named bucket; on a Redis OUTAGE apply the configured outage policy (ADR-021 §1).

    The shared seam for the R7-SEC S3 per-key and per-subscription limits. Default
    ``allow_during_outage=True`` is fail-OPEN (a missing/erroring Redis allows the request, exactly
    like the edge limiter — never self-DoS on an infra blip), but — unlike the old silent swallow —
    EVERY fail-open now emits a structured ``rate_limiter_fail_open`` alert so the bypass is seen.
    ``allow_during_outage=False`` is the hardened opt-in: a Redis outage raises
    ``RateLimiterUnavailable`` (the caller maps it to 503) rather than allowing an un-limited call.
    """

    def _on_outage(reason: str) -> RateLimitDecision:
        _alert_fail_open(namespace=namespace, reason=reason, fail_closed=not allow_during_outage)
        if not allow_during_outage:
            raise RateLimiterUnavailable(reason)
        return RateLimitDecision(allowed=True, retry_after=0)

    if redis is None:
        return _on_outage("redis_unconfigured")
    try:
        return await RateLimitStore(redis).hit(
            identity, limit=limit, window_seconds=window_seconds, namespace=namespace
        )
    except Exception as exc:  # noqa: BLE001 — a redis fault (incl. connect) is an outage, not a 500
        return _on_outage(f"redis_error: {exc}")
