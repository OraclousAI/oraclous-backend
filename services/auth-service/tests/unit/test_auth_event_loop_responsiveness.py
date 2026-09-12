"""Failing test pinning event-loop responsiveness under concurrent bcrypt hashing
(issue #1029, acceptance criterion 2).

auth-service runs a single uvicorn worker / single event loop. Today,
``AgentRepository.create_agent`` calls ``bcrypt.hashpw`` (cost=12, ~185ms)
directly and synchronously inside the ``async def create_agent_credential``
route handler in ``app/factory.py`` — that call runs on the event-loop thread
itself, so for its ~185ms duration NOTHING else can run: no other coroutine
gets scheduled, not even a trivial ``asyncio.sleep`` wakeup or an unrelated
``GET /health`` request. The fix (built separately, not by this test): move
the bcrypt work into a worker thread via a new ``core.password_hashing``
module.

This test proves the *symptom* end-to-end through the real ASGI app rather
than asserting on which thread bcrypt ran on (that is
``test_agent_repository_off_thread.py``'s job) — it is the acceptance-visible
proof that concurrent load is not correctness-affecting for other requests.

Mechanism: fire off 8 concurrent ``POST /internal/agent-credentials`` calls
(each doing one real, unstubbed bcrypt hash inside the handler) together, in
the same ``asyncio.gather``, with:

* a "ticker" coroutine that wakes every 5ms (``asyncio.sleep(0.005)``) and
  records wall-clock timestamps — the classic event-loop-lag probe. If the
  loop is blocked by a synchronous bcrypt call, the ticker cannot be
  scheduled until that call returns, so a gap far larger than 5ms shows up
  between two consecutive wakeups.
* one ``GET /health`` call (trivial, no auth/DB) whose own round-trip latency
  is measured — if the loop is blocked mid-request, ``/health`` queues behind
  whichever bcrypt call is in flight and its latency balloons accordingly.

RED reason on current ``main``: each ``bcrypt.hashpw`` call runs synchronously
on the event-loop thread for ~185ms. With 8 concurrent hashing requests
serializing on the single thread, the loop is blocked for stretches on the
order of 185ms-1.5s, so both the ticker's max gap and the ``/health`` latency
blow past the 150ms thresholds below. Once bcrypt moves to worker threads
(the fix), the event loop stays free to service the ticker and ``/health``
regardless of how many bcrypt calls are in flight, and this test goes GREEN
with no changes here.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_auth_service.app.factory import create_app
from oraclous_auth_service.models.agent_model import Agent, AgentCredential
from oraclous_auth_service.repositories.agent_repository import AgentRepository

pytestmark = [pytest.mark.unit, pytest.mark.slow]

_INTERNAL_KEY = "test-internal-key"
_HASHING_CALLS = 8  # generous margin: even 4 serialized ~185ms calls (740ms) would fail hard
_TICKER_INTERVAL = 0.005  # 5ms — the probe's sleep granularity
_TICKER_ITERATIONS = 200  # ~1s of wall time, long enough to overlap all hashing calls
_LAG_THRESHOLD_S = 0.15  # 150ms
_HEALTH_LATENCY_THRESHOLD_S = 0.15  # 150ms


class _InMemoryCredentialStore:
    """Test double for the agent-credential persistence seam.

    Mirrors ``test_agent_credential_lifecycle.py``'s fake exactly, so
    ``AgentRepository(store=store)`` behaves the same way here. No database
    involved — ``create_agent`` does real, synchronous-today bcrypt work with
    nothing else in the way.
    """

    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.credentials: list[AgentCredential] = []
        self.prefix_lookups: list[str] = []

    async def persist(self, agent: Agent, credential: AgentCredential) -> None:
        self.agents[agent.id] = agent
        self.credentials.append(credential)

    async def active_credentials_by_prefix(self, prefix: str) -> list[AgentCredential]:
        self.prefix_lookups.append(prefix)
        return [
            c for c in self.credentials if c.credential_prefix == prefix and c.status == "active"
        ]

    async def revoke_agent_credentials(self, agent_id: str) -> int:
        count = 0
        for c in self.credentials:
            if c.agent_id == agent_id and c.status == "active":
                c.status = "revoked"
                count += 1
        return count


def _app():
    return create_app(
        agent_repository=AgentRepository(store=_InMemoryCredentialStore()),
        internal_service_key=_INTERNAL_KEY,
    )


async def _create_agent_credential(client: AsyncClient, index: int) -> int:
    """POST /internal/agent-credentials — does one real bcrypt hash server-side."""
    response = await client.post(
        "/internal/agent-credentials",
        headers={"X-Internal-Key": _INTERNAL_KEY},
        json={
            "organisation_id": f"org-{index}",
            "created_by_user_id": f"user-{index}",
        },
    )
    return response.status_code


async def _timed_health(client: AsyncClient) -> float:
    """GET /health — trivial, no auth/DB. Returns its own round-trip latency."""
    start = time.monotonic()
    response = await client.get("/health")
    latency = time.monotonic() - start
    assert response.status_code == 200
    return latency


async def _ticker() -> float:
    """Wake every ``_TICKER_INTERVAL`` seconds and return the largest observed gap.

    A busy but unblocked event loop keeps wakeups close to the requested
    interval (sub-millisecond to a few ms of jitter is normal). A synchronous
    call that blocks the loop delays the *next* wakeup by roughly the
    duration of that call, which shows up here as one large gap.
    """
    timestamps: list[float] = [time.monotonic()]
    for _ in range(_TICKER_ITERATIONS):
        await asyncio.sleep(_TICKER_INTERVAL)
        timestamps.append(time.monotonic())
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False)]
    return max(gaps)


async def test_event_loop_stays_responsive_under_concurrent_bcrypt_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin the bcrypt worker pool size so CPU contention with the event-loop
    # thread is host-independent — the not-yet-built
    # ``core.password_hashing`` module reads this env var (clamped to
    # [2, 8], defaulting from ``os.cpu_count()``); until it lands this
    # setting is inert, since nothing reads it and bcrypt still runs
    # synchronously on the event-loop thread. Fixing it at 2 means a
    # 2-core CI runner and an 8-core laptop see the same number of
    # competing bcrypt threads.
    monkeypatch.setenv("AUTH_PASSWORD_HASH_WORKERS", "2")

    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        results = await asyncio.gather(
            _ticker(),
            _timed_health(client),
            *(_create_agent_credential(client, i) for i in range(_HASHING_CALLS)),
        )

    max_ticker_gap, health_latency, *hashing_statuses = results

    # Sanity check: the concurrency harness itself worked and nothing errored out.
    assert hashing_statuses == [201] * _HASHING_CALLS

    # Real bcrypt (cost=12) is ~185ms per call. If it runs synchronously on the
    # event-loop thread, EVERY other coroutine (including this ticker) is
    # frozen for that long. The pre-fix signal measured here is 1.3-1.5s (and
    # the same order of magnitude on the deployed stack) — 150ms is well
    # below that, so it still fails hard on a real regression, while leaving
    # more headroom than 50ms against ordinary scheduler jitter on a small,
    # shared CI runner where up to 8 bcrypt threads compete for CPU.
    assert max_ticker_gap < _LAG_THRESHOLD_S, (
        f"event loop lagged {max_ticker_gap:.3f}s between ticker wakeups — "
        f"bcrypt is blocking the loop (threshold {_LAG_THRESHOLD_S}s)"
    )

    # If bcrypt blocked the loop, /health would queue behind whichever
    # in-flight hashing call holds the thread — on the order of 185ms-1.5s
    # for 8 concurrent calls serialized. A healthy /health round-trip is
    # sub-millisecond in-process, so 150ms leaves generous room over that
    # while still failing hard against the real bug.
    assert health_latency < _HEALTH_LATENCY_THRESHOLD_S, (
        f"/health took {health_latency:.3f}s while bcrypt hashing was in flight — "
        f"bcrypt is blocking the loop (threshold {_HEALTH_LATENCY_THRESHOLD_S}s)"
    )
