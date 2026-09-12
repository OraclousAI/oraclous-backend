"""Failing unit tests pinning bcrypt work off the event loop (issue #1029).

auth-service runs one event loop; bcrypt (cost=12, ~185ms/call) currently runs
synchronously inside async request handlers, freezing the process for every
other in-flight request. The fix: ``domain.passwords.hash_password`` and
``verify_password`` become ``async def`` and delegate to a new
``core.password_hashing`` module that runs ``bcrypt.hashpw``/``bcrypt.checkpw``
in a worker thread (e.g. via ``asyncio.to_thread``).

What these tests pin:
- ``hash_password`` and ``verify_password`` are coroutine functions;
- the actual bcrypt call happens on a thread other than the main thread —
  proven by monkeypatching ``bcrypt.hashpw``/``bcrypt.checkpw`` (patched as
  module attributes, since ``domain.passwords`` calls them via
  ``bcrypt.<name>(...)`` attribute access, not ``from bcrypt import ...``) with
  wrappers that record ``threading.current_thread()`` before delegating to the
  real implementation, so behaviour is unaffected;
- real bcrypt still round-trips correctly end-to-end (no monkeypatch);
- the existing 72-byte-limit ``PasswordPolicyError`` behaviour survives the
  move to async.

RED until ``oraclous_auth_service.core.password_hashing`` exists and
``domain.passwords.hash_password``/``verify_password`` become ``async def``
delegating to it. On current ``main`` both functions are still synchronous
(return ``str``/``bool`` directly), so every ``await`` below raises
``TypeError: object str/bool can't be used in 'await' expression`` and the
``inspect.iscoroutinefunction`` assertions fail outright.
"""

from __future__ import annotations

import inspect
import threading

import bcrypt
import pytest
from oraclous_auth_service.domain.passwords import (
    PasswordPolicyError,
    hash_password,
    verify_password,
)

pytestmark = pytest.mark.unit


def test_hash_password_is_a_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(hash_password)


def test_verify_password_is_a_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(verify_password)


async def test_hash_password_runs_bcrypt_hashpw_off_the_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hashpw = bcrypt.hashpw
    seen_threads: list[threading.Thread] = []

    def wrapper(secret: bytes, salt: bytes) -> bytes:
        seen_threads.append(threading.current_thread())
        return real_hashpw(secret, salt)

    monkeypatch.setattr(bcrypt, "hashpw", wrapper)

    await hash_password("Sup3rStrong")

    assert seen_threads, "bcrypt.hashpw was never called"
    assert seen_threads[0] is not threading.main_thread()


async def test_verify_password_runs_bcrypt_checkpw_off_the_main_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_checkpw = bcrypt.checkpw
    seen_threads: list[threading.Thread] = []

    def wrapper(secret: bytes, hashed: bytes) -> bool:
        seen_threads.append(threading.current_thread())
        return real_checkpw(secret, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", wrapper)

    stored = bcrypt.hashpw(b"Sup3rStrong", bcrypt.gensalt(rounds=12)).decode("utf-8")

    result = await verify_password("Sup3rStrong", stored)

    assert result is True
    assert seen_threads, "bcrypt.checkpw was never called"
    assert seen_threads[0] is not threading.main_thread()


async def test_real_bcrypt_round_trip_still_works() -> None:
    h = await hash_password("Sup3rStrong")

    assert h != "Sup3rStrong"
    assert await verify_password("Sup3rStrong", h) is True
    assert await verify_password("wrong", h) is False
    assert await verify_password("anything", None) is False


async def test_over_length_password_still_raises_policy_error() -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        await hash_password("x" * 73)

    assert exc_info.value.code == "too_long"
