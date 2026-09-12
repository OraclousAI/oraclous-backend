"""Shared bounded bcrypt worker pool (issue #1029).

bcrypt at cost=12 takes roughly 185ms per call — long enough that running it
on the single asyncio event-loop thread stalls every other request the
service is handling (health checks, unrelated endpoints, everything). It has
to run off the event loop, in a thread pool.

It deliberately does NOT use ``asyncio.to_thread`` (or ``loop.run_in_executor``
with ``executor=None``, which is the same thing): that call goes through the
loop's *default* executor, whose default cap is ``min(32, os.cpu_count() +
4)``. Under concurrent login/signup/agent-token load, that default cap lets
up to that many bcrypt hashes run at once — each one CPU-bound — which
thrashes a small container's CPU just as badly as blocking the event loop
did. Trading an event-loop stall for a CPU-thrashing stampede is not a fix.

This module owns one shared, explicitly bounded
``concurrent.futures.ThreadPoolExecutor`` instead, sized by
``password_hash_max_workers()`` (default: ``os.cpu_count()`` clamped into
``[2, 8]``; overridable via ``AUTH_PASSWORD_HASH_WORKERS`` for operators who
know their container's real budget). Every bcrypt call in the service —
hashing and verifying alike — is routed through this one pool, so the total
number of concurrent bcrypt threads is bounded regardless of how many
requests arrive at once.

Public contract:

- ``BCRYPT_ROUNDS`` — default bcrypt cost factor (12).
- ``bcrypt_hash(secret, *, rounds=BCRYPT_ROUNDS)`` — awaitable, runs
  ``bcrypt.hashpw`` in the shared pool.
- ``bcrypt_verify(secret, hashed)`` — awaitable, runs ``bcrypt.checkpw`` in
  the shared pool; returns ``False`` on a malformed hash (``ValueError``),
  matching the pre-existing ``domain.passwords.verify_password`` behaviour.
- ``password_hash_max_workers()`` — the resolved worker-pool bound, read
  once (at import time) and cached; ``importlib.reload()`` forces
  re-resolution.
- ``get_executor()`` — the one shared, lazily-created executor instance
  every call above runs through.

Both ``bcrypt.hashpw`` and ``bcrypt.checkpw`` are called via module-attribute
access (``bcrypt.hashpw(...)``, never ``from bcrypt import hashpw``) so that
monkeypatching ``bcrypt.hashpw``/``bcrypt.checkpw`` as module attributes — as
the test suite does — is observed at call time.

The executor is a module-level singleton, built lazily under a lock and torn
down via ``atexit`` rather than owned by the FastAPI app's lifespan. It has
to be reachable from ``domain/`` and ``repositories/`` — layers with no
access to the ``app`` object — so a lifespan-owned executor would need to be
threaded down through every call site as an extra parameter, or stashed on
some other ambient singleton. A lazily-created module-level executor,
shut down at process exit, gives every caller the same bounded pool without
that plumbing, at the cost of not being explicitly closed on a graceful
FastAPI shutdown — an acceptable trade for a pool of daemon-adjacent worker
threads whose only job is to finish in-flight bcrypt calls.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import os
import threading

import bcrypt

BCRYPT_ROUNDS = 12

_DEFAULT_MIN_WORKERS = 2
_DEFAULT_MAX_WORKERS = 8

_max_workers_lock = threading.Lock()
_max_workers: int | None = None

_executor_lock = threading.Lock()
_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _resolve_max_workers() -> int:
    raw = os.environ.get("AUTH_PASSWORD_HASH_WORKERS")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass  # nosec B112 - fall through to the CPU-derived default below

    cpu_count = os.cpu_count() or _DEFAULT_MIN_WORKERS
    return min(_DEFAULT_MAX_WORKERS, max(_DEFAULT_MIN_WORKERS, cpu_count))


def password_hash_max_workers() -> int:
    """Return the resolved worker-pool bound, computed once and cached at import time.

    Reload this module (``importlib.reload``) to force re-resolution after changing
    ``AUTH_PASSWORD_HASH_WORKERS``; mutating the environment variable alone has no effect on an
    already-resolved value.
    """
    global _max_workers
    if _max_workers is None:
        with _max_workers_lock:
            if _max_workers is None:
                _max_workers = _resolve_max_workers()
    return _max_workers


def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the one shared bcrypt worker pool, creating it on first use."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=password_hash_max_workers(),
                    thread_name_prefix="bcrypt-worker",
                )
                atexit.register(_executor.shutdown, wait=False)
    return _executor


async def bcrypt_hash(secret: bytes, *, rounds: int = BCRYPT_ROUNDS) -> bytes:
    """Hash ``secret`` with bcrypt (cost=``rounds``) in the shared worker pool."""
    loop = asyncio.get_running_loop()
    salt = bcrypt.gensalt(rounds=rounds)
    return await loop.run_in_executor(get_executor(), bcrypt.hashpw, secret, salt)


async def bcrypt_verify(secret: bytes, hashed: bytes) -> bool:
    """Verify ``secret`` against ``hashed`` in the shared worker pool.

    Returns ``False`` if ``hashed`` is malformed (``ValueError``), matching the pre-existing
    ``domain.passwords.verify_password`` behaviour.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(get_executor(), bcrypt.checkpw, secret, hashed)
    except ValueError:
        return False
