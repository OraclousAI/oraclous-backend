"""Failing unit tests pinning agent-credential bcrypt work off the event loop
(issue #1029).

``AgentRepository.create_agent`` and ``AgentRepository.validate_credential``
are already ``async def``, but each calls ``bcrypt.hashpw``/``bcrypt.checkpw``
directly and synchronously on whatever thread invokes them — the calling
(event-loop) thread in production. The fix: both calls move into a new
``core.password_hashing`` module (``bcrypt_hash``/``bcrypt_verify``) that runs
the real bcrypt work in a worker thread (e.g. via ``asyncio.to_thread``), and
``agent_repository.py`` awaits them instead of calling ``bcrypt`` directly.

What these tests pin:
- the actual bcrypt call inside ``create_agent`` happens on a thread other
  than the main thread — proven by monkeypatching ``bcrypt.hashpw`` (patched
  as a module attribute, since ``agent_repository.py`` calls it via
  ``bcrypt.hashpw(...)`` attribute access) with a wrapper that records
  ``threading.current_thread()`` before delegating to the real implementation;
- the same for ``bcrypt.checkpw`` inside ``validate_credential``;
- behaviour is otherwise unchanged: the returned raw credential still
  round-trips through ``validate_credential``, and a wrong/garbled credential
  still returns ``None`` without raising.

RED until ``oraclous_auth_service.core.password_hashing`` exists and
``agent_repository.py``'s ``create_agent``/``validate_credential`` delegate to
it instead of calling ``bcrypt.hashpw``/``bcrypt.checkpw`` directly. On current
``main`` both calls run synchronously on the calling thread, so
``threading.current_thread() is threading.main_thread()`` and the
``is not threading.main_thread()`` assertions below fail.
"""

from __future__ import annotations

import threading

import bcrypt
import pytest
from oraclous_auth_service.models.agent_model import Agent, AgentCredential
from oraclous_auth_service.repositories.agent_repository import AgentRepository

pytestmark = pytest.mark.unit

_ORG = "org-aaaa"
_USER = "user-1234"


class _InMemoryCredentialStore:
    """Test double for the agent-credential persistence seam.

    Mirrors ``test_agent_credential_lifecycle.py``'s fake exactly, so
    ``AgentRepository(store=store)`` behaves the same way here.
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


@pytest.fixture
def store() -> _InMemoryCredentialStore:
    return _InMemoryCredentialStore()


@pytest.fixture
def repo(store: _InMemoryCredentialStore) -> AgentRepository:
    return AgentRepository(store=store)


async def test_create_agent_hashes_credential_off_the_main_thread(
    repo: AgentRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hashpw = bcrypt.hashpw
    seen_threads: list[threading.Thread] = []

    def wrapper(secret: bytes, salt: bytes) -> bytes:
        seen_threads.append(threading.current_thread())
        return real_hashpw(secret, salt)

    monkeypatch.setattr(bcrypt, "hashpw", wrapper)

    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)

    assert seen_threads, "bcrypt.hashpw was never called"
    assert seen_threads[0] is not threading.main_thread()

    # Behaviour is unaffected: the credential still round-trips.
    assert await repo.validate_credential(raw) == agent.id


async def test_validate_credential_checks_off_the_main_thread(
    repo: AgentRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)

    real_checkpw = bcrypt.checkpw
    seen_threads: list[threading.Thread] = []

    def wrapper(secret: bytes, hashed: bytes) -> bool:
        seen_threads.append(threading.current_thread())
        return real_checkpw(secret, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", wrapper)

    result = await repo.validate_credential(raw)

    assert result == agent.id
    assert seen_threads, "bcrypt.checkpw was never called"
    assert seen_threads[0] is not threading.main_thread()


async def test_validate_credential_still_returns_none_for_a_wrong_credential(
    repo: AgentRepository,
) -> None:
    """A malformed/garbled credential still returns ``None``, never raises."""
    raw, _agent = await repo.create_agent(organisation_id=_ORG, created_by_user_id=_USER)
    tampered = raw[:-1] + ("A" if raw[-1] != "A" else "B")

    assert await repo.validate_credential(tampered) is None
