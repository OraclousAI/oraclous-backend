"""Failing unit tests pinning the shared bounded bcrypt worker pool (issue #1029).

``oraclous_auth_service.core.password_hashing`` does not exist yet — it is the
module ``domain.passwords`` and ``agent_repository.py`` will delegate to once
bcrypt work moves off the event loop (see the sibling ``*_off_thread.py`` and
``test_auth_event_loop_responsiveness.py`` files for the caller-side pinning).
This file pins the module's *own* contract directly: a single shared,
BOUNDED ``concurrent.futures.ThreadPoolExecutor`` owned by the module itself
— explicitly not ``asyncio.to_thread``'s default executor, whose default
ceiling of ``min(32, os.cpu_count() + 4)`` would let unbounded bcrypt threads
spawn and thrash a small container's CPU.

Every reference to ``oraclous_auth_service.core.password_hashing`` below is a
function-local import (never at module level), per
``.claude/rules/tests-seam-imports.md``: a module-level import of a
not-yet-built intra-repo seam aborts pytest COLLECTION for the whole repo
(exit code 2), reddening every other open PR. Importing inside each test body
instead means the file always collects cleanly and each test fails at
*runtime* with ``ModuleNotFoundError: No module named
'oraclous_auth_service.core.password_hashing'`` — RED by design, on this
file's own marker only. This is expected and correct until the `[impl]` for
#1029 lands; it is not a bug to fix.

The module's pinned public contract (implementer: match these names exactly):

- ``BCRYPT_ROUNDS: int`` — default cost factor (12).
- ``async def bcrypt_hash(secret: bytes, *, rounds: int = BCRYPT_ROUNDS) -> bytes``
- ``async def bcrypt_verify(secret: bytes, hashed: bytes) -> bool``
- ``password_hash_max_workers() -> int`` — the resolved worker-pool bound:
  read once from env var ``AUTH_PASSWORD_HASH_WORKERS``, defaulting to
  ``os.cpu_count()`` clamped into ``[2, 8]``.
- ``get_executor() -> concurrent.futures.ThreadPoolExecutor`` — the one
  shared executor instance the module hashes/verifies through. Returning the
  SAME instance across calls (until the module is reloaded) is exactly what
  "singleton pool" means here.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import threading

import pytest
from oraclous_telemetry import DegradationEvent, register_sink, reset_sinks

pytestmark = pytest.mark.unit


@pytest.fixture
def captured_alerts():
    """Capture every ``alert(...)`` fired during a test (mirrors the pattern in
    ``services/application-gateway-service/tests/unit/test_rate_limit_store.py``)."""
    events: list[DegradationEvent] = []
    reset_sinks()
    register_sink(events.append)
    yield events
    reset_sinks()


async def test_bcrypt_hash_reuses_one_shared_executor_instance() -> None:
    from oraclous_auth_service.core import password_hashing

    await password_hashing.bcrypt_hash(b"pw-singleton-1")
    first = password_hashing.get_executor()

    await password_hashing.bcrypt_hash(b"pw-singleton-2")
    second = password_hashing.get_executor()

    assert first is second, "bcrypt_hash must reuse one pool, not create a new one per call"


async def test_default_worker_bound_is_resolved_and_clamped_to_2_through_8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_PASSWORD_HASH_WORKERS", raising=False)

    from oraclous_auth_service.core import password_hashing

    bound = password_hashing.password_hash_max_workers()

    assert 2 <= bound <= 8

    await password_hashing.bcrypt_hash(b"pw-bound-check")
    executor = password_hashing.get_executor()

    assert executor._max_workers == bound  # noqa: SLF001 - only way to assert the real pool size


async def test_concurrent_load_never_uses_more_threads_than_the_resolved_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_PASSWORD_HASH_WORKERS", raising=False)

    import bcrypt
    from oraclous_auth_service.core import password_hashing

    real_hashpw = bcrypt.hashpw
    seen_thread_names: set[str] = set()
    lock = threading.Lock()

    def wrapper(secret: bytes, salt: bytes) -> bytes:
        with lock:
            seen_thread_names.add(threading.current_thread().name)
        return real_hashpw(secret, salt)

    monkeypatch.setattr(bcrypt, "hashpw", wrapper)

    await asyncio.gather(*(password_hashing.bcrypt_hash(f"pw-{i}".encode()) for i in range(20)))

    max_workers = password_hashing.password_hash_max_workers()

    assert seen_thread_names, "bcrypt.hashpw was never called"
    assert len(seen_thread_names) <= max_workers, (
        f"observed {len(seen_thread_names)} distinct worker threads under load, "
        f"which exceeds the resolved bound of {max_workers}"
    )


async def test_env_var_overrides_the_default_worker_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oraclous_auth_service.core import password_hashing

    original_env = os.environ.get("AUTH_PASSWORD_HASH_WORKERS")
    try:
        monkeypatch.setenv("AUTH_PASSWORD_HASH_WORKERS", "3")
        # The bound is read ONCE (at import time or lazily-then-cached), so
        # mutating the env var alone would not be observed — force
        # re-resolution by reloading the module with the new value in place.
        importlib.reload(password_hashing)

        assert password_hashing.password_hash_max_workers() == 3
    finally:
        if original_env is None:
            monkeypatch.delenv("AUTH_PASSWORD_HASH_WORKERS", raising=False)
        else:
            monkeypatch.setenv("AUTH_PASSWORD_HASH_WORKERS", original_env)
        # Reload again so the module's cached bound doesn't leak the
        # test-only value of 3 into any test that runs after this one in the
        # same process.
        importlib.reload(password_hashing)


async def test_zero_workers_clamps_to_the_override_floor_of_one(
    monkeypatch: pytest.MonkeyPatch, captured_alerts: list[DegradationEvent]
) -> None:
    from oraclous_auth_service.core import password_hashing

    monkeypatch.setenv("AUTH_PASSWORD_HASH_WORKERS", "0")
    importlib.reload(password_hashing)

    assert password_hashing.password_hash_max_workers() == 1

    fired = [e for e in captured_alerts if e.code == "password_hash_workers_invalid"]
    assert len(fired) == 1, "an out-of-range override must be reported, not silently clamped"
    assert fired[0].context["raw_value"] == "0"
    assert fired[0].context["effective_value"] == 1
    assert fired[0].context["reason"] == "out_of_range"


async def test_500_workers_clamps_to_the_override_ceiling_of_64(
    monkeypatch: pytest.MonkeyPatch, captured_alerts: list[DegradationEvent]
) -> None:
    from oraclous_auth_service.core import password_hashing

    monkeypatch.setenv("AUTH_PASSWORD_HASH_WORKERS", "500")
    importlib.reload(password_hashing)

    assert password_hashing.password_hash_max_workers() == 64

    fired = [e for e in captured_alerts if e.code == "password_hash_workers_invalid"]
    assert len(fired) == 1, "an out-of-range override must be reported, not silently clamped"
    assert fired[0].context["raw_value"] == "500"
    assert fired[0].context["effective_value"] == 64
    assert fired[0].context["reason"] == "out_of_range"


async def test_unparseable_value_falls_back_to_the_cpu_derived_default(
    monkeypatch: pytest.MonkeyPatch, captured_alerts: list[DegradationEvent]
) -> None:
    from oraclous_auth_service.core import password_hashing

    monkeypatch.setenv("AUTH_PASSWORD_HASH_WORKERS", "notanumber")
    importlib.reload(password_hashing)

    bound = password_hashing.password_hash_max_workers()

    assert 2 <= bound <= 8, "a rejected override must fall back to the CPU-derived default"

    fired = [e for e in captured_alerts if e.code == "password_hash_workers_invalid"]
    assert len(fired) == 1, "a rejected override must be reported, not silently ignored"
    assert fired[0].context["raw_value"] == "notanumber"
    assert fired[0].context["effective_value"] == bound
    assert fired[0].context["reason"] == "not_an_integer"
