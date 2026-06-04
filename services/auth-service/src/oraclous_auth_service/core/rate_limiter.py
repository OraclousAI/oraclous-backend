"""Per-credential-prefix rate limiter for ``POST /agent-token`` (ORA-31).

Reshaped from the legacy ``auth-service/app/core/rate_limiter.enforce_key_prefix_rate_limit``:

* The body field is ``credential`` (legacy: ``api_key``); the 12-char prefix
  window is preserved.
* The Redis key namespace is agent-specific (``rl:agent:pfx:...``) so it does
  not collide with the legacy SA limiter's ``rl:pfx:`` namespace.
* INCR + EXPIRE go through a transactional pipeline so a crash between them
  cannot leave a key without a TTL (permanently rate-limiting that prefix).
* On any Redis error or absence of ``app.state.redis`` the check is skipped —
  a Redis outage must not lock every agent out (legacy precedent).
* A missing / empty / malformed-JSON ``credential`` short-circuits BEFORE any
  access to ``app.state.redis``; the endpoint's 401 path handles invalid
  credentials, not this dependency.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

# Mirrors the legacy 12-character key_prefix window and per-prefix rate.
_PREFIX_WINDOW_LEN = 12
_PREFIX_LIMIT = 10
_PREFIX_WINDOW_SECONDS = 60
_REDIS_KEY_NS = "rl:agent:pfx:"


def _extract_credential(body_bytes: bytes) -> str:
    """Return the ``credential`` field from the request body, or an empty string.

    A missing / empty / malformed-JSON body collapses to ``""`` so the caller
    can short-circuit without touching Redis. Any exception decoding the body
    is treated as "no credential to limit on" — invalid-credential handling
    belongs to the endpoint, not the limiter.
    """
    try:
        data: Any = json.loads(body_bytes)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    raw = data.get("credential", "") or ""
    return raw if isinstance(raw, str) else ""


async def enforce_agent_credential_prefix_rate_limit(request: Request) -> None:
    """FastAPI dependency: cap requests per credential-prefix window.

    Raises ``HTTPException(429)`` with a ``Retry-After`` header when the limit
    is exceeded; otherwise returns silently. Fail-open on any infrastructure
    fault (missing Redis, Redis error) — the legacy precedent.
    """
    body_bytes = await request.body()
    credential = _extract_credential(body_bytes)
    prefix = credential[:_PREFIX_WINDOW_LEN]
    if not prefix:
        # Short-circuit BEFORE any app.state access — be-test-reviewer's
        # non-blocking note: malformed/empty bodies must never touch Redis,
        # otherwise the empty-body test passes for the wrong reason.
        return

    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        logger.warning("rate_limiter: Redis not available, skipping prefix check")
        return

    redis_key = f"{_REDIS_KEY_NS}{prefix}"
    try:
        # MULTI/EXEC pipeline so INCR + EXPIRE execute atomically; without this,
        # a crash between INCR and EXPIRE leaves the key with no TTL.
        async with redis_client.pipeline(transaction=True) as pipe:
            await pipe.incr(redis_key)
            await pipe.expire(redis_key, _PREFIX_WINDOW_SECONDS)
            results = await pipe.execute()
        count = int(results[0])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open per legacy precedent
        logger.error("rate_limiter: Redis error during prefix check: %s", exc)
        return

    if count > _PREFIX_LIMIT:
        try:
            ttl = await redis_client.ttl(redis_key)
        except Exception:  # noqa: BLE001 — fall back to minimum retry
            ttl = 1
        retry_after = max(int(ttl) if ttl is not None else 1, 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for this credential prefix. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )


_INVITE_KEY_NS = "rl:invite:pfx:"


def _extract_token(body_bytes: bytes) -> str:
    """Return the ``token`` field from the request body, or an empty string (fail-open shape)."""
    try:
        data: Any = json.loads(body_bytes)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    raw = data.get("token", "") or ""
    return raw if isinstance(raw, str) else ""


async def enforce_invitation_token_prefix_rate_limit(request: Request) -> None:
    """Cap invitation peek/accept attempts per token-prefix window (T-INVITE brute-force guard).

    Same window + fail-open-on-Redis-fault discipline as the agent limiter, on a distinct namespace
    and the ``token`` body field.
    """
    token = _extract_token(await request.body())
    prefix = token[:_PREFIX_WINDOW_LEN]
    if not prefix:
        return
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        logger.warning("rate_limiter: Redis not available, skipping invitation prefix check")
        return
    redis_key = f"{_INVITE_KEY_NS}{prefix}"
    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            await pipe.incr(redis_key)
            await pipe.expire(redis_key, _PREFIX_WINDOW_SECONDS)
            results = await pipe.execute()
        count = int(results[0])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open per legacy precedent
        logger.error("rate_limiter: Redis error during invitation prefix check: %s", exc)
        return
    if count > _PREFIX_LIMIT:
        try:
            ttl = await redis_client.ttl(redis_key)
        except Exception:  # noqa: BLE001 — fall back to minimum retry
            ttl = 1
        retry_after = max(int(ttl) if ttl is not None else 1, 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for this invitation token. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
