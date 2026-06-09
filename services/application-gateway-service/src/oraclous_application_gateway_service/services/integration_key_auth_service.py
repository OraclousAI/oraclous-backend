"""Integration-key validation (ORAA-4 §21 services layer) — the inbound authz floor (ADR-019).

Resolves an ``oak-``/``oag-`` bearer to an authenticated Principal WITHOUT a JWT: prefix lookup →
constant-time hash compare → status/TTL checks → mint a SERVICE_ACCOUNT Principal under the key's
bound organisation. Fail-closed: any miss / revoked / expired / bad-secret raises ``AuthError`` (→
401) before any upstream call. The minted ``organisation_id`` is guaranteed non-None (the store
column is NOT NULL), which the proxy's strip-then-assert anti-spoof relies on to scope the request.

The per-key BINDING (a bound agent slug XOR a capability allow-list) is carried out of ``resolve``
on a ``ResolvedKey`` so the Slice-4 published-agent invoke route can enforce it pre-forward (403
unless the key's bound slug matches the invoked agent). The live edge auth (Slice 3) now calls
``resolve`` too; ``resolve_principal`` is a retained identity-only back-compat wrapper.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from oraclous_governance import Principal, PrincipalType

from oraclous_application_gateway_service.core.auth import AuthError
from oraclous_application_gateway_service.domain.integration_key import prefix_of, verify_key
from oraclous_application_gateway_service.repositories.integration_key_repository import (
    IntegrationKeyRepository,
)

_DUMMY_HASH = "0" * 64  # a sha256-width value to compare against on a prefix miss (constant-time)


@dataclass(frozen=True)
class ResolvedKey:
    """An authenticated integration key: principal + the binding the invoke route enforces + the
    per-key CORS allow-list the published-agent middleware applies (Slice 5)."""

    principal: Principal
    key_id: uuid.UUID
    bound_agent_slug: str | None
    capability_allow_list: list[str] | None
    cors_origins: list[str] | None
    rate_limit: int | None = None  # per-key request cap (R7-SEC S3); None = no per-key limit
    rate_window_seconds: int | None = None


class IntegrationKeyAuthService:
    def __init__(self, repository: IntegrationKeyRepository) -> None:
        self._repo = repository

    async def resolve_principal(self, token: str) -> Principal:
        """Identity only — a retained back-compat wrapper; the live edge auth calls ``resolve``."""
        return (await self.resolve(token)).principal

    async def resolve(self, token: str) -> ResolvedKey:
        prefix = prefix_of(token)
        if prefix is None:
            raise AuthError("malformed integration key")
        row = await self._repo.get_by_prefix(prefix)
        # constant-time compare even on a miss (a fixed dummy hash) so an unknown prefix and a
        # known-prefix-wrong-secret are timing-indistinguishable; the response is generic anyway.
        if not verify_key(token, row.key_hash if row is not None else _DUMMY_HASH) or row is None:
            raise AuthError("invalid integration key")
        if row.status != "active":
            raise AuthError("integration key is revoked")
        if row.expires_at is not None and _utcnow() >= _as_aware(row.expires_at):
            raise AuthError("integration key has expired")
        # org is guaranteed non-None by the NOT NULL store column (anti-spoof depends on this)
        principal = Principal(
            principal_id=row.id,
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            organisation_id=row.organisation_id,
        )
        return ResolvedKey(
            principal=principal,
            key_id=row.id,
            bound_agent_slug=row.bound_agent_slug,
            capability_allow_list=row.capability_allow_list,
            cors_origins=row.cors_origins,
            # getattr: the real model always has these columns; a partial projection/fake without
            # them resolves to None = no per-key limit (the safe default).
            rate_limit=getattr(row, "rate_limit", None),
            rate_window_seconds=getattr(row, "rate_window_seconds", None),
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
