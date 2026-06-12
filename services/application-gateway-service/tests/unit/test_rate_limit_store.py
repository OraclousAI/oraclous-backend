"""Unit: the shared fail-open rate-limit helper (R7-SEC S3). No real Redis.

Also covers the ADR-021 §1 / #296 failure-mode DoD: a Redis outage no longer fails open SILENTLY —
it emits a structured ``rate_limiter_fail_open`` alert (default fail-open behaviour preserved); and
the opt-in ``allow_during_outage=False`` flips the outage to fail-CLOSED (raises
``RateLimiterUnavailable``).
"""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.repositories.rate_limit_store import (
    RateLimiterUnavailable,
    enforce_bucket,
)
from oraclous_telemetry import DegradationEvent, register_sink, reset_sinks

pytestmark = pytest.mark.unit


@pytest.fixture
def captured_alerts():
    events: list[DegradationEvent] = []
    reset_sinks()
    register_sink(events.append)
    yield events
    reset_sinks()


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


# --- ADR-021 §1 / #296 failure-mode DoD --------------------------------------------------------


async def test_redis_outage_fail_open_fires_the_alert(captured_alerts) -> None:
    """A simulated Redis outage still fails OPEN (default behaviour preserved) but is now VISIBLE:
    a structured ``rate_limiter_fail_open`` WARNING fires instead of the old silent swallow."""
    d = await enforce_bucket(
        _BoomRedis(), identity="x", limit=1, window_seconds=60, namespace="rl:t:"
    )
    assert d.allowed is True  # default fail-open preserved — never self-DoS on a blip
    fired = [e for e in captured_alerts if e.code == "rate_limiter_fail_open"]
    assert len(fired) == 1
    assert fired[0].severity == "warning"
    assert fired[0].context["fail_closed"] is False
    assert fired[0].context["namespace"] == "rl:t:"


async def test_redis_absent_fail_open_fires_the_alert(captured_alerts) -> None:
    d = await enforce_bucket(None, identity="x", limit=1, window_seconds=60, namespace="rl:t:")
    assert d.allowed is True
    assert any(e.code == "rate_limiter_fail_open" for e in captured_alerts)


async def test_allow_during_outage_false_fails_closed(captured_alerts) -> None:
    """The hardened opt-in: a Redis outage with ``allow_during_outage=False`` REFUSES the request
    (raises ``RateLimiterUnavailable`` -> the caller 503s) and fires the alert as an ERROR."""
    with pytest.raises(RateLimiterUnavailable):
        await enforce_bucket(
            _BoomRedis(),
            identity="x",
            limit=1,
            window_seconds=60,
            namespace="rl:t:",
            allow_during_outage=False,
        )
    fired = [e for e in captured_alerts if e.code == "rate_limiter_fail_open"]
    assert fired and fired[0].severity == "error" and fired[0].context["fail_closed"] is True


async def test_redis_absent_fail_closed_raises() -> None:
    with pytest.raises(RateLimiterUnavailable):
        await enforce_bucket(
            None,
            identity="x",
            limit=1,
            window_seconds=60,
            namespace="rl:t:",
            allow_during_outage=False,
        )


async def test_healthy_redis_does_not_alert(captured_alerts) -> None:
    """A working Redis path emits NO alert — the signal is outage-only, not per-request."""
    await enforce_bucket(_FakeRedis(), identity="x", limit=5, window_seconds=60, namespace="rl:t:")
    assert not [e for e in captured_alerts if e.code == "rate_limiter_fail_open"]
