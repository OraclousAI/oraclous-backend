"""Unit: the shared fail-open rate-limit helper (R7-SEC S3). No real Redis."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.repositories.rate_limit_store import enforce_bucket

pytestmark = pytest.mark.unit


class _Pipe:
    def __init__(self, redis) -> None:  # noqa: ANN001
        self._r = redis

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *_a) -> bool:  # noqa: ANN002
        return False

    async def incr(self, _key: str) -> None:
        self._r.n += 1

    async def expire(self, _key: str, _w: int) -> None:
        return None

    async def execute(self) -> list[int]:
        return [self._r.n]


class _FakeRedis:
    def __init__(self) -> None:
        self.n = 0
        self.keys: list[str] = []

    def pipeline(self, transaction: bool = True):  # noqa: ANN201, ARG002, FBT001, FBT002
        return _Pipe(self)

    async def ttl(self, key: str) -> int:
        self.keys.append(key)
        return 30


class _BoomRedis:
    def pipeline(self, transaction: bool = True):  # noqa: ANN201, ARG002, FBT001, FBT002
        raise RuntimeError("redis down")


async def test_fail_open_when_redis_is_absent() -> None:
    d = await enforce_bucket(None, identity="x", limit=1, window_seconds=60, namespace="rl:t:")
    assert d.allowed is True


async def test_fail_open_on_a_redis_error() -> None:
    d = await enforce_bucket(
        _BoomRedis(), identity="x", limit=1, window_seconds=60, namespace="rl:t:"
    )
    assert d.allowed is True  # never self-DoS on an infra blip


async def test_allows_under_the_limit_then_denies_over() -> None:
    r = _FakeRedis()
    first = await enforce_bucket(r, identity="sub-1", limit=2, window_seconds=60, namespace="rl:t:")
    second = await enforce_bucket(
        r, identity="sub-1", limit=2, window_seconds=60, namespace="rl:t:"
    )
    third = await enforce_bucket(r, identity="sub-1", limit=2, window_seconds=60, namespace="rl:t:")
    assert first.allowed and second.allowed  # count 1,2 <= 2
    assert not third.allowed and third.retry_after >= 1  # count 3 > 2 -> denied + Retry-After
    assert r.keys[-1] == "rl:t:sub-1"  # the namespaced bucket key
