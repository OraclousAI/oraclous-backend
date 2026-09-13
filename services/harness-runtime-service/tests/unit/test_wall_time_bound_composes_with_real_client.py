"""#1067 (test-quality review, PR #1071) — the loop's OWN wall-time bound and the client's OWN
wall-clock bound must COMPOSE correctly when both are real.

No existing test wired the real ``OpenAICompatibleClient`` into ``run_tool_use_loop`` — every loop
test uses a fake LLM, and the client's own test drives ``complete()`` directly, never through the
loop. That leaves the seam between them unproven: the loop bounds ``await llm.complete(...)`` with
its OWN ``asyncio.wait_for`` (the run's remaining wall-time budget); the client bounds its own HTTP
call with a SEPARATE ``asyncio.wait_for`` (``self._timeout``) one layer further in. When the loop's
bound is the SMALLER of the two and fires first, the resulting cancellation travels THROUGH the
client's own ``await asyncio.wait_for(self._client.post(...), timeout=self._timeout)`` — which has
its own ``except TimeoutError`` handler for exactly that exception type.

``asyncio.CancelledError`` (what a foreign/outer cancellation actually raises at that await point)
is a ``BaseException``, not an ``Exception``, so it is not one Python's builtin ``TimeoutError``
(an ``OSError``/``Exception`` subclass) can ever catch — the client's own handler only fires on ITS
OWN timeout elapsing, never on a cancellation arriving from outside. If that boundary were ever
blurred (e.g. by a broadened ``except Exception`` somewhere in between converting the cancellation
into an ordinary transient ``LLMClientError``), the run would report a plain retryable error
instead of its own wall-time budget being spent, and the terminal a person reads would be wrong.
This test proves the composition holds with the REAL client, not just by code inspection.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from oraclous_harness_runtime_service.domain.llm.openai_compatible import OpenAICompatibleClient
from oraclous_harness_runtime_service.domain.loop.tool_use import run_tool_use_loop
from oraclous_harness_runtime_service.domain.policy import PolicyEnvelope
from oraclous_harness_runtime_service.models.enums import HarnessStatus

pytestmark = [pytest.mark.unit, pytest.mark.tool_dispatch]

# The CLIENT's own configured wall-clock bound — deliberately LARGE relative to the loop's budget
# below, so the LOOP's own outer bound is what actually fires first (the scenario this test exists
# to prove composes correctly).
_CLIENT_TIMEOUT_SECONDS = 10.0
# The run's own wall-clock budget — small, and strictly under the client's own bound above.
_LOOP_MAX_WALL_SECONDS = 1
# How often the server drips a keep-alive byte — irrelevant to which bound fires first here (both
# bounds are wall-clock, not per-read), but mirrors the packet-capture shape from the sibling test.
_DRIP_INTERVAL_SECONDS = 0.25
# The outer test-level deadline so a real hang FAILS this test rather than wedging the whole run.
_OUTER_DEADLINE_SECONDS = _LOOP_MAX_WALL_SECONDS * 12


async def _drip_forever(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Send a normal HTTP/1.1 200 response's headers immediately, then dribble one body byte at a
    time forever — never enough to satisfy Content-Length, never closing. Same shape as
    test_openai_compatible_wall_clock_bound.py's server."""
    try:
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        header = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 1000000\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        writer.write(header)
        await writer.drain()
        while True:
            writer.write(b" ")
            await writer.drain()
            await asyncio.sleep(_DRIP_INTERVAL_SECONDS)
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def test_the_loops_own_bound_wins_when_smaller_and_reports_wall_time_not_a_transient_error() -> (
    None
):
    server = await asyncio.start_server(_drip_forever, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{port}",
            api_key="sk-test",
            model="vendor/model",
            timeout=_CLIENT_TIMEOUT_SECONDS,
        )
        try:
            policy = PolicyEnvelope(
                max_iterations=6,
                max_tool_calls=None,
                max_wall_time_seconds=_LOOP_MAX_WALL_SECONDS,
                max_tokens=None,
            )
            async with asyncio.timeout(_OUTER_DEADLINE_SECONDS):
                result = await run_tool_use_loop(
                    llm=client,
                    system="",
                    user_input="go",
                    tool_specs=[],
                    dispatch=lambda spec, args: None,  # type: ignore[arg-type,return-value]
                    policy=policy,
                )
        finally:
            await client.aclose()

    # The composition this test exists to prove: the LOOP's own (smaller) bound fired, not the
    # client's — a plain transient LLMClientError-driven FAILED would mean the cancellation was
    # swallowed somewhere on the way through the client's own wait_for.
    assert result.status is HarnessStatus.ESCALATED, result
    assert result.error_type == "wall_time", result
    assert result.error_message is not None
    assert "WallTimeBudgetExhausted" not in result.error_message
