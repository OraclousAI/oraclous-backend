"""Delegated-token service layer (R1-B1).

The broker enforces, on every use of a delegated token, that:

* the requesting principal matches the token's bound agent (T2 — leaked
  delegated token unusable by any other agent),
* the token has not expired,
* the requested scopes are a subset of the delegated scopes (T2 core — no
  scope creep),
* the token has not been revoked,
* the caller's ``organisation_id`` matches the token's persisted
  ``organisation_id`` (ADR-006 — cross-org rejected as ``unknown`` to avoid
  leaking the token's existence across the tenant boundary).

The raw bearer value is generated once at mint, returned to the caller, and
never persisted in clear — only a stable lookup prefix and a SHA-256 hash are
stored (AC4). Hashing is via ``hashlib.sha256`` rather than bcrypt because
delegated tokens are short-lived, opaque, internally-issued bearers — the
legacy SA/agent-credential bcrypt envelope is for long-lived
human/agent-presented credentials.

The persistence ``store`` is a port: any object honouring the four async
methods ``persist``, ``get_by_prefix_for_org``, ``get_by_id_for_org``,
``mark_revoked`` is acceptable. The unit tests pass an in-memory double; the
Postgres-backed store is a follow-up integration story.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

_TOKEN_SCHEME = "odt_"  # noqa: S105 — scheme tag, not a secret
_RAW_RANDOM_BYTES = 24  # ~32 url-safe chars; keeps a comfortable margin against guessing
_PREFIX_LEN = 12  # "odt_" + 8 chars of the random body — wide enough for prefix-index uniqueness


@dataclass
class DelegatedTokenRecord:
    """The in-memory shape the service constructs and the store persists.

    Mutable: the expiry-rejection unit test mutates ``record.expires_at`` on a
    just-minted record to simulate elapsed time without sleeping (the test
    double keeps a reference to the same object).
    """

    id: uuid.UUID
    organisation_id: uuid.UUID
    member_id: uuid.UUID
    agent_id: uuid.UUID
    scopes: frozenset[str]
    expires_at: datetime
    status: str  # "active" | "revoked"
    token_hash: str
    token_prefix: str


@dataclass(frozen=True)
class DelegationValidation:
    """Outcome of a per-use validation.

    On success, the bound metadata is echoed back for the caller to authorise
    the downstream action; on rejection, ``reason`` carries the discriminant
    (audit-loggable, observable in tests). The raw bearer is **never** carried
    on this result — AC4 internal-only invariant.

    ``reason`` values pinned by the test contract:

    * ``"agent_mismatch"`` — requesting principal differs from the bound agent
    * ``"expired"`` — the token's ``expires_at`` is in the past at validation
      time
    * ``"scope_creep"`` — at least one requested scope falls outside the
      delegated subset (T2 core)
    * ``"revoked"`` — the token has been revoked
    * ``"unknown"`` — no row matches the bearer (also used for cross-org to
      avoid information leak about token existence)
    """

    success: bool
    reason: str | None = None
    token_id: uuid.UUID | None = None
    member_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    granted_scopes: frozenset[str] | None = None


class _DelegatedTokenStore(Protocol):
    """The persistence port. The unit tests pass an in-memory double; the
    Postgres-backed implementation is a follow-up integration story."""

    async def persist(self, token: DelegatedTokenRecord) -> None: ...

    async def get_by_prefix_for_org(
        self, prefix: str, organisation_id: uuid.UUID
    ) -> DelegatedTokenRecord | None: ...

    async def get_by_id_for_org(
        self, token_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> DelegatedTokenRecord | None: ...

    async def mark_revoked(self, token_id: uuid.UUID, organisation_id: uuid.UUID) -> int: ...


def _generate_raw_token() -> str:
    return _TOKEN_SCHEME + secrets.token_urlsafe(_RAW_RANDOM_BYTES)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prefix_of(raw: str) -> str:
    return raw[:_PREFIX_LEN]


class DelegationService:
    def __init__(self, *, store: _DelegatedTokenStore) -> None:
        self._store = store

    async def mint(
        self,
        *,
        organisation_id: uuid.UUID,
        member_id: uuid.UUID,
        agent_id: uuid.UUID,
        scopes: Iterable[str],
        expires_at: datetime,
    ) -> tuple[str, DelegatedTokenRecord]:
        """Mint a fresh delegated token bound to (member, agent, scopes, expiry).

        Returns ``(raw_token, record)``. ``raw_token`` is the *only* path by
        which the bearer bytes leave the broker; nothing on ``record`` echoes
        them in clear (only ``token_hash`` and ``token_prefix``).
        """
        scopes_set = frozenset(scopes)
        if not scopes_set:
            # AC1 fail-closed: a token with no delegated scopes has no
            # authority and cannot be scope-creep-checked; refuse rather than
            # persist a dead row.
            raise ValueError("scopes must not be empty")

        if expires_at <= datetime.now(UTC):
            # A pre-expired token would be a footgun for the caller; refuse at
            # mint time rather than persist a dead row.
            raise ValueError("expires_at must be in the future")

        raw = _generate_raw_token()

        record = DelegatedTokenRecord(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            member_id=member_id,
            agent_id=agent_id,
            scopes=scopes_set,
            expires_at=expires_at,
            status="active",
            token_hash=_hash_token(raw),
            token_prefix=_prefix_of(raw),
        )
        await self._store.persist(record)
        return raw, record

    async def validate(
        self,
        *,
        raw_token: str,
        organisation_id: uuid.UUID,
        requesting_agent_id: uuid.UUID,
        requested_scopes: Iterable[str],
    ) -> DelegationValidation:
        """Per-use validation. Returns a discriminating outcome.

        Order of checks is deliberate:

        1. Prefix lookup scoped by ``organisation_id`` — a cross-org caller
           gets ``unknown`` (no information leak about the token's existence
           in another tenant).
        2. Hash equality — a same-prefix tampered bearer also reads as
           ``unknown``.
        3. Status — revoked tokens are denied with their own reason (audit
           discriminant).
        4. Expiry — expired tokens are denied; checked at every use so the
           runtime cannot trust the agent's clock.
        5. Agent binding — a different agent presenting the token is denied
           (T2 leaked-token mitigation).
        6. Scope subset — any requested scope outside the delegated set is
           scope_creep (T2 core).
        """
        prefix = _prefix_of(raw_token)
        row = await self._store.get_by_prefix_for_org(prefix, organisation_id)
        # Constant-time hash comparison (WP-11): a plain ``!=`` on the hex digest leaks, via timing,
        # how many leading chars a guessed bearer shares with the stored hash. ``compare_digest``
        # removes that side channel; behaviour is identical (equal hashes → match). The
        # short-circuit is kept on the ``row is None`` arm only (no secret to compare on no match).
        if row is None or not hmac.compare_digest(row.token_hash, _hash_token(raw_token)):
            # Cross-org and tampered/unknown both fall here. Returning
            # ``unknown`` (not ``org_mismatch``) is the information-leak-safe
            # default per the be-test-reviewer disposition on cross-org.
            return DelegationValidation(success=False, reason="unknown")

        if row.status == "revoked":
            return DelegationValidation(success=False, reason="revoked")

        if row.expires_at <= datetime.now(UTC):
            return DelegationValidation(success=False, reason="expired")

        if row.agent_id != requesting_agent_id:
            return DelegationValidation(success=False, reason="agent_mismatch")

        requested = frozenset(requested_scopes)
        if not requested.issubset(row.scopes):
            return DelegationValidation(success=False, reason="scope_creep")

        return DelegationValidation(
            success=True,
            token_id=row.id,
            member_id=row.member_id,
            agent_id=row.agent_id,
            granted_scopes=frozenset(row.scopes),
        )

    async def revoke(self, *, token_id: uuid.UUID, organisation_id: uuid.UUID) -> int:
        """Revoke a delegated token. Idempotent; returns the number of rows changed.

        Cross-org callers receive 0 (no row matched in their org) — same
        information-leak-safe default as ``validate``.
        """
        return await self._store.mark_revoked(token_id, organisation_id)
