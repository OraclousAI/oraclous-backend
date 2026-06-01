"""Repo-root conftest: put the repository root on sys.path so `tools` imports."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _preserve_event_loop():
    """
    Restore the session event loop if a sync test body calls asyncio.run().

    asyncio.run() calls asyncio.set_event_loop(None) on exit (Python 3.10+).
    With asyncio_default_test_loop_scope=session, all async tests share one
    session event loop. A sync test that calls asyncio.run() (e.g.
    test_agent_identity_backfill.py) will clear that loop, causing all
    subsequent async tests to fail with 'There is no current event loop'.

    This fixture captures the current loop before each test and re-registers
    it if asyncio.run() deregistered it, so the next async test picks up the
    same open loop.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    yield
    if loop is not None and not loop.is_closed():
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(loop)
