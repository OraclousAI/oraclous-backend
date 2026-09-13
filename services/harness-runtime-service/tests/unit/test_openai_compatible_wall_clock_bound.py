"""#1067 (R1, item 1) — ONE model call must be bounded by WALL-CLOCK time, not per-read time.

Packet capture (settled, see the issue): OpenRouter answers with response HEADERS in ~1s, then
drips a ~35-byte keep-alive packet every ~3s while it queues the real completion — the BODY can
arrive minutes later. ``OpenAICompatibleClient.__init__`` (openai_compatible.py:185-197) passes its
``timeout`` as a bare ``float`` to ``httpx.AsyncClient``; httpx expands a scalar into
connect/read/write/pool timeouts of that same value, and the READ timeout is reset on every chunk
received. A drip arriving faster than the read timeout therefore resets it forever — the bound can
never fire, no matter how small it is set.

This test drives the REAL client against a REAL local TCP server (no monkeypatched httpx, no
``httpx.MockTransport``) that sends response headers immediately and then dribbles single bytes
forever, slower than nothing yet still well inside the configured bound's read-timeout window. A
correct implementation bounds the WHOLE call by wall-clock time and raises ``LLMClientError``
(transient=True) at/near that bound. Today's implementation never notices the drip and hangs
forever, so this test wraps the call in an explicit outer deadline (``asyncio.wait_for``, several
times the configured bound) so a hang FAILS the test instead of wedging the whole suite.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from oraclous_harness_runtime_service.domain.llm.openai_compatible import (
    LLMClientError,
    OpenAICompatibleClient,
)

pytestmark = pytest.mark.unit

# The call-level wall-clock bound under test. Small so the test runs fast; the drip interval below
# is deliberately SHORTER than this, which is exactly what lets the bug reproduce at this scale.
_BOUND_SECONDS = 1.0
# How often the server drips a keep-alive byte — shorter than _BOUND_SECONDS, so today's
# read-timeout-per-chunk behaviour never fires.
_DRIP_INTERVAL_SECONDS = 0.25
# The outer test-level deadline so a real hang FAILS this test rather than wedging the whole run.
# Generous relative to _BOUND_SECONDS (not a hand-derived exact number) so the assertion is about
# "bounded", not about racing a tight clock.
_OUTER_DEADLINE_SECONDS = _BOUND_SECONDS * 8


async def _drip_forever(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Send a normal HTTP/1.1 200 response's headers immediately, then dribble one body byte at a
    time forever — never sending enough bytes to satisfy Content-Length, and never closing. This is
    the packet-capture shape: headers fast, body a drip of tiny packets, no end in sight."""
    try:
        # Drain the request (headers + any body) before responding, so httpx's own request write
        # completes cleanly; we don't need to parse it, just stop reading once we hit the blank
        # line that ends the headers.
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
        with __import__("contextlib").suppress(Exception):
            writer.close()


async def test_a_single_model_call_is_bounded_by_wall_clock_not_per_read_time() -> None:
    server = await asyncio.start_server(_drip_forever, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = OpenAICompatibleClient(
            base_url=f"http://127.0.0.1:{port}",
            api_key="sk-test",
            model="vendor/model",
            timeout=_BOUND_SECONDS,
        )
        try:
            started = time.monotonic()
            try:
                async with asyncio.timeout(_OUTER_DEADLINE_SECONDS):
                    with pytest.raises(LLMClientError) as exc_info:
                        await client.complete(
                            messages=[{"role": "user", "content": "go"}], system="", tools=[]
                        )
            except TimeoutError:
                pytest.fail(
                    f"the call was still hanging after {_OUTER_DEADLINE_SECONDS}s (outer test "
                    f"deadline), even though it was configured with a {_BOUND_SECONDS}s bound — a "
                    f"keep-alive drip every {_DRIP_INTERVAL_SECONDS}s (shorter than the bound) "
                    "keeps resetting httpx's per-read timeout forever. There is no wall-clock "
                    "bound on a single model call (openai_compatible.py:185-197 passes `timeout` "
                    "as a bare scalar, which httpx expands into a READ timeout that restarts on "
                    "every received chunk)."
                )
            elapsed = time.monotonic() - started
            # transient: a bounded retry may recover from a call that merely ran long.
            assert exc_info.value.transient is True
            # bounded by the CONFIGURED wall-clock limit, with generous headroom — never anywhere
            # near the outer test deadline (which exists only so a still-broken implementation
            # fails fast instead of wedging the suite).
            assert elapsed < _OUTER_DEADLINE_SECONDS / 2, (
                f"the call took {elapsed:.2f}s against a configured bound of {_BOUND_SECONDS}s — "
                "not bounded by wall-clock time"
            )
        finally:
            await client.aclose()
