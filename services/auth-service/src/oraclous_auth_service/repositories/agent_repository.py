"""Agent principal + credential lifecycle repository (R1-A1).

Reshaped from the legacy ``ServiceAccountRepository``. The raw credential is
generated here and returned exactly once; only its bcrypt hash and a prefix
index are persisted. Persistence is delegated to an injected
:class:`CredentialStore` port so the credential logic is unit-testable without a
database — the real Postgres-backed store is a separate (integration) concern.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Protocol

import bcrypt

from oraclous_auth_service.models.agent_model import Agent, AgentCredential

_CREDENTIAL_PREFIX = "oag_"
_PREFIX_INDEX_LEN = 12  # "oag_" + 8 chars — mirrors the legacy 12-char key prefix
_BCRYPT_ROUNDS = 12
_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_TOKEN_BYTES = 32
_TOKEN_LEN = 43  # base62 width of 32 random bytes


class CredentialStore(Protocol):
    """Persistence port for agents and their credentials.

    ``active_credentials_by_prefix`` returns only ``status == "active"`` rows
    (the status filter lives in the store / SQL); credential **expiry** is
    evaluated by the repository.
    """

    async def persist(self, agent: Agent, credential: AgentCredential) -> None: ...

    async def active_credentials_by_prefix(self, prefix: str) -> list[AgentCredential]: ...

    async def revoke_agent_credentials(self, agent_id: str) -> int: ...

    async def organisation_id_for(self, agent_id: str) -> str | None: ...

    async def principal_type_for(self, agent_id: str) -> str | None: ...


def _generate_credential() -> tuple[str, str]:
    """Return ``(raw_credential, prefix)`` — ``oag_`` + base62(32 random bytes)."""
    n = int.from_bytes(secrets.token_bytes(_TOKEN_BYTES), "big")
    chars: list[str] = []
    while n:
        chars.append(_BASE62[n % 62])
        n //= 62
    token = "".join(reversed(chars)).rjust(_TOKEN_LEN, "0")
    raw = f"{_CREDENTIAL_PREFIX}{token}"
    return raw, raw[:_PREFIX_INDEX_LEN]


class AgentRepository:
    """Create, validate, and revoke agent credentials against a credential store."""

    def __init__(self, store: CredentialStore) -> None:
        self._store = store

    async def create_agent(
        self,
        *,
        organisation_id: str,
        created_by_user_id: str,
        principal_type: str = "agent",
        expires_at: datetime | None = None,
    ) -> tuple[str, Agent]:
        """Create an agent + its first credential; return ``(raw_credential, agent)``.

        The raw credential is available only from this return value — the stored
        record holds a bcrypt hash and a prefix index, never the secret.
        """
        raw, prefix = _generate_credential()
        credential_hash = bcrypt.hashpw(
            raw.encode(), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
        ).decode()

        agent = Agent(
            id=str(uuid.uuid4()),
            organisation_id=organisation_id,
            created_by_user_id=created_by_user_id,
        )
        credential = AgentCredential(
            id=str(uuid.uuid4()),
            agent_id=agent.id,
            organisation_id=organisation_id,
            principal_type=principal_type,
            credential_hash=credential_hash,
            credential_prefix=prefix,
            status="active",
            expires_at=expires_at,
            revoked_at=None,
        )
        await self._store.persist(agent, credential)
        return raw, agent

    async def validate_credential(self, raw_credential: str) -> str | None:
        """Return the owning ``agent_id`` for a valid credential, else ``None``.

        Guards the prefix before any store lookup, then resolves candidates via
        the prefix index and confirms with a bcrypt verify. Expired credentials
        are skipped; revoked ones never reach here (the store returns only active
        rows).
        """
        if not raw_credential.startswith(_CREDENTIAL_PREFIX):
            return None

        prefix = raw_credential[:_PREFIX_INDEX_LEN]
        candidates = await self._store.active_credentials_by_prefix(prefix)
        now = datetime.now(UTC)
        for candidate in candidates:
            if candidate.expires_at is not None and candidate.expires_at < now:
                continue
            if bcrypt.checkpw(raw_credential.encode(), candidate.credential_hash.encode()):
                return candidate.agent_id
        return None

    async def revoke_agent(self, agent_id: str) -> int:
        """Revoke all active credentials for an agent; return the number revoked."""
        return await self._store.revoke_agent_credentials(agent_id)

    async def organisation_id_for(self, agent_id: str) -> str | None:
        """Return the agent's org iff it still has an active credential, else ``None`` (T2)."""
        return await self._store.organisation_id_for(agent_id)

    async def principal_type_for(self, agent_id: str) -> str | None:
        """Return the principal_type (agent|service_account) of an active credential, else None."""
        return await self._store.principal_type_for(agent_id)
