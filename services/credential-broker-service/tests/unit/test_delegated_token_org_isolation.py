"""Failing org-isolation tests for the delegated-token surface (R1-B1).

The delegated-token primitive sits on the Substrate side of the four-layer
model: per ADR-006, every read and write the broker performs must be scoped by
``organisation_id``. These tests pin the cross-organisation rejection at the
service-layer seam — a token minted in org A is *not* validatable when the
authenticated caller asserts org B, even when the bearer bytes match.

Companion to ``test_delegation_service.py``: that file covers same-org
behaviour (scope creep, agent mismatch, expiry, revocation); this file covers
the org boundary specifically.

Threat reference: Structured Threat Catalogue T1-M2 / T2 isolation envelope —
a leaked delegated token must not enable cross-organisation traversal even if
the caller knows the bearer value.

RED until ``backend-implementer`` creates
``oraclous_credential_broker_service.services.delegation_service`` with
``organisation_id`` enforced on every read in the validation path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.organization_isolation]


_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_MEMBER = uuid.UUID("33333333-3333-3333-3333-333333333333")
_AGENT = uuid.UUID("44444444-4444-4444-4444-444444444444")
_DELEGATED_SCOPES = frozenset({"drive.read"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _StoredToken:
    id: uuid.UUID
    organisation_id: uuid.UUID
    member_id: uuid.UUID
    agent_id: uuid.UUID
    scopes: frozenset[str]
    expires_at: datetime
    status: str
    token_hash: str
    token_prefix: str


@dataclass
class _InMemoryDelegatedTokenStore:
    """Same in-memory store contract as test_delegation_service.py.

    Duplicated rather than imported so the two files stay independent and
    self-contained — each test file should be readable as a single artefact
    per the test-author skill's "tests are independent" criterion.
    """

    rows: list[_StoredToken] = field(default_factory=list)
    prefix_lookups: list[str] = field(default_factory=list)

    async def persist(self, token: _StoredToken) -> None:
        self.rows.append(token)

    async def get_by_prefix_for_org(
        self, prefix: str, organisation_id: uuid.UUID
    ) -> _StoredToken | None:
        self.prefix_lookups.append(prefix)
        for row in self.rows:
            if row.token_prefix == prefix and row.organisation_id == organisation_id:
                return row
        return None

    async def get_by_id_for_org(
        self, token_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> _StoredToken | None:
        for row in self.rows:
            if row.id == token_id and row.organisation_id == organisation_id:
                return row
        return None

    async def mark_revoked(self, token_id: uuid.UUID, organisation_id: uuid.UUID) -> int:
        count = 0
        for row in self.rows:
            if (
                row.id == token_id
                and row.organisation_id == organisation_id
                and row.status == "active"
            ):
                row.status = "revoked"
                count += 1
        return count


@pytest.fixture
def store() -> _InMemoryDelegatedTokenStore:
    return _InMemoryDelegatedTokenStore()


@pytest.fixture
def service(store: _InMemoryDelegatedTokenStore):
    from oraclous_credential_broker_service.services.delegation_service import DelegationService

    return DelegationService(store=store)


async def test_validate_rejects_cross_organisation_use_even_with_correct_raw_token(
    service,
) -> None:
    """A token minted in org A is rejected when validated in org B's context.

    The bearer bytes are correct; the agent matches; the scopes are a valid
    subset; the token has not expired. The *only* difference is the
    ``organisation_id`` the caller asserts. The broker rejects because the
    persisted row's ``organisation_id`` does not match, treating the cross-org
    use exactly like an unknown token (no information leak — the rejection
    reason does not reveal that the token exists elsewhere).
    """
    raw, _record = await service.mint(
        organisation_id=_ORG_A,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG_B,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    assert result.success is False
    assert result.reason in {"unknown", "org_mismatch"}, (
        f"cross-org validation must reject without success — got reason={result.reason!r}"
    )


async def test_revoke_rejects_cross_organisation_caller(service) -> None:
    """``revoke(token_id, org_id)`` must not succeed when org_id is wrong.

    Cross-org revocation would be a primitive for harvesting token existence
    across the tenant boundary; the broker treats the wrong-org id as a
    no-op (revoked count = 0) rather than surfacing the existence of the row.
    """
    _raw, record = await service.mint(
        organisation_id=_ORG_A,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    assert await service.revoke(token_id=record.id, organisation_id=_ORG_B) == 0
    # The same call against the correct org still succeeds.
    assert await service.revoke(token_id=record.id, organisation_id=_ORG_A) == 1


async def test_mint_stamps_organisation_id_on_persisted_row(service, store) -> None:
    """ADR-006 storage invariant: ``organisation_id`` is taken from the call argument.

    The service receives ``organisation_id`` from the authenticated caller's
    context (never trusts a request body for it — ORG001 guardrail) and stamps
    it onto the persisted row.
    """
    _raw, record = await service.mint(
        organisation_id=_ORG_A,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    assert record.organisation_id == _ORG_A
    (row,) = store.rows
    assert row.organisation_id == _ORG_A
