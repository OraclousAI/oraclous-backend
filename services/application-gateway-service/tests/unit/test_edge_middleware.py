"""Unit: the edge ASGI middlewares — size guard (fail-closed) + rate limit (fail-open)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from oraclous_application_gateway_service.core.edge_middleware import (
    RateLimitMiddleware,
    SizeGuardMiddleware,
)

pytestmark = pytest.mark.unit


class _StubApp:
    """A downstream app that drains the body (like the proxy) and 200s."""

    def __init__(self) -> None:
        self.called = False
        self.body = b""

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        self.called = True
        while True:
            message = await receive()
            if message["type"] == "http.request":
                self.body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            else:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _drive(
    mw, *, headers=None, client=("1.2.3.4", 0), body_messages=None, app=None, path="/v1/search"
):
    scope = {
        "type": "http",
        "path": path,
        "headers": headers or [],
        "client": client,
        "state": {"request_id": "req_test"},
        "app": app,
    }
    queue = list(body_messages or [{"type": "http.request", "body": b"", "more_body": False}])

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _envelope(sent):
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return json.loads(raw)["error"]


def _header(sent, name: bytes):
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"]).get(name)


# ---- size guard (fail-closed) -------------------------------------------------------------------


async def test_oversize_content_length_413_fast_path() -> None:
    stub = _StubApp()
    mw = SizeGuardMiddleware(stub, max_bytes=100)
    sent = await _drive(mw, headers=[(b"content-length", b"101")])
    assert _status(sent) == 413
    assert _envelope(sent)["code"] == "PAYLOAD_TOO_LARGE"
    assert _envelope(sent)["retryable"] is False
    assert _header(sent, b"x-request-id") == b"req_test"
    assert stub.called is False  # rejected before the app saw the body


async def test_chunked_oversize_413_via_byte_counter() -> None:
    # no Content-Length; oversize arrives in chunks — the counter (not the header) catches it
    stub = _StubApp()
    mw = SizeGuardMiddleware(stub, max_bytes=100)
    chunks = [
        {"type": "http.request", "body": b"x" * 60, "more_body": True},
        {"type": "http.request", "body": b"y" * 60, "more_body": False},
    ]
    sent = await _drive(mw, body_messages=chunks)
    assert _status(sent) == 413
    assert _envelope(sent)["code"] == "PAYLOAD_TOO_LARGE"
    assert stub.called is False


async def test_under_limit_passes_through_with_body_intact() -> None:
    stub = _StubApp()
    mw = SizeGuardMiddleware(stub, max_bytes=100)
    chunks = [
        {"type": "http.request", "body": b"hello ", "more_body": True},
        {"type": "http.request", "body": b"world", "more_body": False},
    ]
    sent = await _drive(mw, body_messages=chunks)
    assert _status(sent) == 200
    assert stub.called is True
    assert stub.body == b"hello world"  # replayed intact


# ---- rate limit (fail-open) ---------------------------------------------------------------------


class _FakePipe:
    def __init__(self, redis) -> None:
        self._redis = redis
        self._ops: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def incr(self, key) -> None:  # noqa: ANN001
        self._ops.append(("incr", key))

    async def expire(self, key, ttl) -> None:  # noqa: ANN001
        self._ops.append(("expire", key))

    async def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "incr":
                self._redis.counts[op[1]] = self._redis.counts.get(op[1], 0) + 1
                self._redis.keys_seen.append(op[1])
                out.append(self._redis.counts[op[1]])
            else:
                out.append(True)
        return out


class _FakeRedis:
    def __init__(self, ttl: int = 42) -> None:
        self.counts: dict[str, int] = {}
        self.keys_seen: list[str] = []
        self._ttl = ttl

    def pipeline(self, transaction: bool = True):  # noqa: FBT001, FBT002
        return _FakePipe(self)

    async def ttl(self, key) -> int:  # noqa: ANN001
        return self._ttl


class _RaisingRedis:
    def pipeline(self, transaction: bool = True):  # noqa: FBT001, FBT002
        raise ConnectionError("redis down")


def _app_with_redis(redis):  # noqa: ANN001
    return SimpleNamespace(state=SimpleNamespace(redis=redis))


async def test_under_limit_allows() -> None:
    stub = _StubApp()
    mw = RateLimitMiddleware(stub, limit=3, window_seconds=60, trusted_proxy_count=0)
    sent = await _drive(mw, app=_app_with_redis(_FakeRedis()))
    assert _status(sent) == 200
    assert stub.called is True


async def test_over_limit_429_with_retry_after() -> None:
    redis = _FakeRedis(ttl=30)  # shared count lives in redis, not the middleware
    app = _app_with_redis(redis)
    await _drive(
        RateLimitMiddleware(_StubApp(), limit=2, window_seconds=60, trusted_proxy_count=0), app=app
    )  # 1
    await _drive(
        RateLimitMiddleware(_StubApp(), limit=2, window_seconds=60, trusted_proxy_count=0), app=app
    )  # 2
    over_stub = _StubApp()
    sent = await _drive(
        RateLimitMiddleware(over_stub, limit=2, window_seconds=60, trusted_proxy_count=0), app=app
    )  # 3 -> over
    assert _status(sent) == 429
    assert _envelope(sent)["code"] == "RATE_LIMITED"
    assert _envelope(sent)["retryable"] is True
    assert _header(sent, b"retry-after") == b"30"
    assert over_stub.called is False  # rejected without reaching the app


async def test_xff_default_shares_one_bucket_regardless_of_spoofed_header() -> None:
    # tpc=0: a rotating XFF must NOT make new buckets — same socket peer -> one key
    redis = _FakeRedis()
    mw = RateLimitMiddleware(_StubApp(), limit=100, window_seconds=60, trusted_proxy_count=0)
    app = _app_with_redis(redis)
    await _drive(mw, app=app, headers=[(b"x-forwarded-for", b"1.1.1.1")])
    await _drive(mw, app=app, headers=[(b"x-forwarded-for", b"2.2.2.2")])
    assert set(redis.keys_seen) == {"rl:edge:ip:1.2.3.4"}  # the socket peer, both times


async def test_exempt_paths_bypass_the_limiter() -> None:
    # /health is exempt: even with a redis that would report over-limit, it is never throttled
    redis = _FakeRedis()
    redis.counts["rl:edge:ip:1.2.3.4"] = 10_000  # already way over any limit
    stub = _StubApp()
    mw = RateLimitMiddleware(stub, limit=1, window_seconds=60, trusted_proxy_count=0)
    sent = await _drive(mw, app=_app_with_redis(redis), path="/health")
    assert _status(sent) == 200
    assert stub.called is True
    assert redis.keys_seen == []  # the limiter was not even consulted


async def test_fail_open_when_redis_is_none() -> None:
    stub = _StubApp()
    mw = RateLimitMiddleware(stub, limit=1, window_seconds=60, trusted_proxy_count=0)
    sent = await _drive(mw, app=_app_with_redis(None))
    assert _status(sent) == 200
    assert stub.called is True  # allowed despite no redis


async def test_fail_open_when_redis_raises() -> None:
    stub = _StubApp()
    mw = RateLimitMiddleware(stub, limit=1, window_seconds=60, trusted_proxy_count=0)
    sent = await _drive(mw, app=_app_with_redis(_RaisingRedis()))
    assert _status(sent) == 200
    assert stub.called is True  # connect-on-use error -> fail open
