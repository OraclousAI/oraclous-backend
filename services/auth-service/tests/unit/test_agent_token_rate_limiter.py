"""Failing unit tests for the ``/agent-token`` per-credential-prefix rate limiter
(ORA-31 / R1-A2).

What these tests pin (ORA-31 acceptance criterion: ``/agent-token`` is
rate-limited with the same shape as ``/service-token``):

* The dependency reads the ``credential`` field from the request body and
  keys the limit on its first 12 characters (mirrors the legacy 12-char
  ``key_prefix`` window).
* INCR + EXPIRE are queued in a single Redis pipeline (atomic) — without this,
  a crash between INCR and EXPIRE leaves a key with no TTL and permanently
  rate-limits the prefix.
* On the 11th request in a window the dependency raises ``HTTPException`` 429
  carrying ``Retry-After`` from the Redis TTL.
* ``Retry-After`` is at least 1 second even when Redis returns 0 or negative.
* The dependency is **fail-open**: a missing Redis client or a Redis error must
  not block authentication — a Redis outage would otherwise lock every agent
  out (legacy precedent).
* A missing/empty/malformed credential is skipped entirely; the endpoint's
  401 path handles invalid credentials, not the rate limiter.

Behavioural reference (Lift): legacy
``auth-service/app/core/rate_limiter.enforce_key_prefix_rate_limit``. Reshape:
key on the ``credential`` body field (the prefix scheme is ``oag_``, but the
limiter never parses scheme); the 12-char prefix-window invariant is preserved.

These tests are RED until
``oraclous_auth_service.core.rate_limiter.enforce_agent_credential_prefix_rate_limit``
exists.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit


def _make_pipeline_request(
    incr_result: int,
    ttl_result: int = 45,
    credential: str = "oag_AbCdEfGh1234XYZ",
    pipeline_error: Exception | None = None,
):
    """Build a mock Starlette ``Request`` whose Redis client uses a pipeline."""
    body = json.dumps({"credential": credential}).encode()

    request = MagicMock()
    request.body = AsyncMock(return_value=body)

    pipe_mock = AsyncMock()
    pipe_mock.incr = AsyncMock(return_value=None)
    pipe_mock.expire = AsyncMock(return_value=None)
    if pipeline_error is not None:
        pipe_mock.execute = AsyncMock(side_effect=pipeline_error)
    else:
        pipe_mock.execute = AsyncMock(return_value=[incr_result, True])

    pipeline_ctx = MagicMock()
    pipeline_ctx.__aenter__ = AsyncMock(return_value=pipe_mock)
    pipeline_ctx.__aexit__ = AsyncMock(return_value=None)

    redis_mock = AsyncMock()
    redis_mock.pipeline = MagicMock(return_value=pipeline_ctx)
    redis_mock.ttl = AsyncMock(return_value=ttl_result)

    app_state = MagicMock()
    app_state.redis = redis_mock
    request.app.state = app_state

    return request, pipe_mock, redis_mock


def _make_no_redis_request(credential: str = "oag_AbCdEfGh1234XYZ"):
    body = json.dumps({"credential": credential}).encode()
    request = MagicMock()
    request.body = AsyncMock(return_value=body)
    app_state = MagicMock()
    app_state.redis = None
    request.app.state = app_state
    return request


async def test_allows_requests_under_limit() -> None:
    """Requests below the threshold (count <= 10) pass through."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request, _, _ = _make_pipeline_request(incr_result=5)
    await enforce_agent_credential_prefix_rate_limit(request)  # no raise


async def test_blocks_on_eleventh_request_with_retry_after() -> None:
    """The 11th request in a window raises 429 with ``Retry-After`` from Redis TTL."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request, _, _ = _make_pipeline_request(incr_result=11, ttl_result=45)
    with pytest.raises(HTTPException) as exc_info:
        await enforce_agent_credential_prefix_rate_limit(request)

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "45"


async def test_retry_after_minimum_is_one_second() -> None:
    """``Retry-After`` is at least 1 even when TTL returns 0."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request, _, _ = _make_pipeline_request(incr_result=15, ttl_result=0)
    with pytest.raises(HTTPException) as exc_info:
        await enforce_agent_credential_prefix_rate_limit(request)

    assert exc_info.value.headers["Retry-After"] == "1"


async def test_fails_open_when_redis_unavailable() -> None:
    """No Redis on ``app.state`` → check skipped (fail open)."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request = _make_no_redis_request()
    await enforce_agent_credential_prefix_rate_limit(request)  # no raise


async def test_fails_open_on_redis_error() -> None:
    """If the Redis pipeline raises, the request is allowed through (legacy precedent)."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request, _, _ = _make_pipeline_request(
        incr_result=1, pipeline_error=ConnectionError("Redis down")
    )
    await enforce_agent_credential_prefix_rate_limit(request)  # no raise


async def test_skips_check_for_empty_credential() -> None:
    """Missing/empty ``credential`` skips the limiter entirely (endpoint returns 401)."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request, _, redis_mock = _make_pipeline_request(incr_result=1, credential="")
    await enforce_agent_credential_prefix_rate_limit(request)

    redis_mock.pipeline.assert_not_called()


async def test_skips_check_on_malformed_body() -> None:
    """Malformed JSON body is treated as empty credential — skipped, no error raised."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request = MagicMock()
    request.body = AsyncMock(return_value=b"not valid json")
    redis_mock = AsyncMock()
    request.app.state.redis = redis_mock

    await enforce_agent_credential_prefix_rate_limit(request)
    redis_mock.pipeline.assert_not_called()


async def test_prefix_window_is_first_twelve_chars() -> None:
    """The Redis key window pins to the first 12 chars of the credential."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    credential = "oag_AbCdEfGh1234_extra_suffix"
    request, pipe_mock, _ = _make_pipeline_request(incr_result=1, credential=credential)
    await enforce_agent_credential_prefix_rate_limit(request)

    expected_prefix = credential[:12]
    (call_args,), _ = pipe_mock.incr.await_args
    assert expected_prefix in call_args
    # Namespace separation: the agent limiter must NOT collide with the SA limiter,
    # so the Redis key carries an agent-specific namespace prefix. The exact
    # spelling is the implementer's choice (e.g. ``rl:agent:pfx:`` vs ``rl:apfx:``);
    # all we assert is that it is *not* the legacy ``rl:pfx:`` SA namespace.
    assert not call_args.startswith("rl:pfx:")


async def test_incr_and_expire_executed_atomically_in_pipeline() -> None:
    """INCR + EXPIRE must be queued in the same pipeline so they execute atomically."""
    from oraclous_auth_service.core.rate_limiter import (
        enforce_agent_credential_prefix_rate_limit,
    )

    request, pipe_mock, _ = _make_pipeline_request(incr_result=1)
    await enforce_agent_credential_prefix_rate_limit(request)

    pipe_mock.incr.assert_awaited_once()
    pipe_mock.expire.assert_awaited_once()
    pipe_mock.execute.assert_awaited_once()
