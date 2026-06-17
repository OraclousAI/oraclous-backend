"""Postgres-backed implementation of the ``_DelegatedTokenStore`` protocol
(ORA-37 / R1 gate).

The ORA-32 unit suite pins the in-memory store under which ``DelegationService``
already passes; this is the production-backed companion the R1 adversarial
suite (``tests/security/test_adversarial_delegation.py``) drives — the only
way to prove forgery / expiry / scope-creep rejection at the data layer, on
the 0d real-substrate harness.

ADR-006: every read and write carries ``organisation_id``. The two get-by-*
methods are *scoped by org* on the WHERE — a cross-organisation lookup returns
None (the information-leak-safe ``unknown`` discriminant the service surfaces
to the caller).

Each call opens a fresh connection from the injected ``AsyncEngine`` — no
per-store state (no cached sessions). Keeps the store concurrency-safe and
matches the engine-injection pattern the broker tests use.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import Table, select, update

from oraclous_credential_broker_service.models.delegated_token import DelegatedToken
from oraclous_credential_broker_service.services.delegation_service import (
    DelegatedTokenRecord,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


# A mapped class's ``__table__`` is typed ``FromClause`` (lacks ``.insert()`` / is not an
# ``update()`` target), but is a ``Table`` at runtime — narrow it so the Core DML below type-checks.
_TABLE: Table = cast("Table", DelegatedToken.__table__)


class PostgresDelegatedTokenStore:
    """Async SQLAlchemy-backed ``_DelegatedTokenStore`` over real Postgres."""

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    async def persist(self, token: DelegatedTokenRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                _TABLE.insert().values(
                    id=token.id,
                    organisation_id=token.organisation_id,
                    member_id=token.member_id,
                    agent_id=token.agent_id,
                    # ARRAY(String) — Postgres stores a list, the service-layer
                    # record uses frozenset. Sort for deterministic at-rest
                    # ordering (helps human-readable audits).
                    scopes=sorted(token.scopes),
                    expires_at=token.expires_at,
                    status=token.status,
                    token_hash=token.token_hash,
                    token_prefix=token.token_prefix,
                )
            )

    async def get_by_prefix_for_org(
        self, prefix: str, organisation_id: uuid.UUID
    ) -> DelegatedTokenRecord | None:
        async with self._engine.connect() as conn:
            stmt = (
                select(_TABLE)
                .where(
                    _TABLE.c.token_prefix == prefix,
                    _TABLE.c.organisation_id == organisation_id,
                )
                .limit(1)
            )
            row = (await conn.execute(stmt)).mappings().first()
        return _row_to_record(row) if row is not None else None

    async def get_by_id_for_org(
        self, token_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> DelegatedTokenRecord | None:
        async with self._engine.connect() as conn:
            stmt = (
                select(_TABLE)
                .where(
                    _TABLE.c.id == token_id,
                    _TABLE.c.organisation_id == organisation_id,
                )
                .limit(1)
            )
            row = (await conn.execute(stmt)).mappings().first()
        return _row_to_record(row) if row is not None else None

    async def mark_revoked(self, token_id: uuid.UUID, organisation_id: uuid.UUID) -> int:
        """Soft-revoke (``status = 'revoked'``). Returns the number of rows
        flipped — 0 if the row is missing, already revoked, or belongs to
        another organisation. Matches the in-memory store's semantics
        exactly (only active rows count).
        """
        async with self._engine.begin() as conn:
            stmt = (
                update(_TABLE)
                .where(
                    _TABLE.c.id == token_id,
                    _TABLE.c.organisation_id == organisation_id,
                    _TABLE.c.status == "active",
                )
                .values(status="revoked")
            )
            result = await conn.execute(stmt)
        return result.rowcount


def _row_to_record(row: Any) -> DelegatedTokenRecord:
    return DelegatedTokenRecord(
        id=row["id"],
        organisation_id=row["organisation_id"],
        member_id=row["member_id"],
        agent_id=row["agent_id"],
        scopes=frozenset(row["scopes"]),
        expires_at=row["expires_at"],
        status=row["status"],
        token_hash=row["token_hash"],
        token_prefix=row["token_prefix"],
    )
