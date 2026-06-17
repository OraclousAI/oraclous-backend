"""Failing tests for the ``DelegationService`` (ORA-32 / R1-B1).

Pins the per-use validation behaviour the broker must enforce on every use of a
delegated token. The acceptance criteria from the brief, mapped to tests:

* AC1 — *A member mints a delegated token for an agent with a scope subset;
  token stored in the broker.* → ``test_mint_*``
* AC2 — *Per-use validation: requesting principal must match the token's agent;
  expiry enforced.* → ``test_validate_accepts_*``,
  ``test_validate_rejects_agent_mismatch``, ``test_validate_rejects_expired_*``
* AC3 — *An action exceeding the delegated scope is rejected (scope creep) —
  test proves it.* → ``test_validate_rejects_scope_creep_*``
* AC4 — *Tokens are internal-only (never reach external providers).* →
  ``test_validation_result_does_not_carry_raw_token`` (the service exposes the
  raw bytes *only* via the one-shot mint return value; nothing on the rejection
  or success result echoes them back)

Threat reference: Structured Threat Catalogue **T2** — scope creep is the core
T2 failure mode for agents; the broker is the layer that enforces the cap.

These tests describe behaviour at the service-layer seam, not its persistence:
``_InMemoryDelegatedTokenStore`` is a test double mirroring the
``_InMemoryCredentialStore`` idiom established in ORA-30
(``services/auth-service/tests/unit/test_agent_credential_lifecycle.py``). The
Postgres-backed store is deferred to a follow-up integration story.

RED until ``backend-implementer`` creates:

* ``oraclous_credential_broker_service.services.delegation_service.DelegationService``
* ``oraclous_credential_broker_service.services.delegation_service.DelegationValidation``
* a store port the service constructs against (call shape exercised here).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]


_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_AGENT = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
_OTHER_AGENT = uuid.UUID("00000000-0000-0000-0000-0000000000dd")

_DELEGATED_SCOPES = frozenset({"drive.read", "calendar.read"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _StoredToken:
    """The shape the in-memory store keeps for each minted token."""

    id: uuid.UUID
    organisation_id: uuid.UUID
    member_id: uuid.UUID
    agent_id: uuid.UUID
    scopes: frozenset[str]
    expires_at: datetime
    status: str  # "active" | "revoked"
    token_hash: str
    token_prefix: str


@dataclass
class _InMemoryDelegatedTokenStore:
    """Test double for the delegated-token persistence seam.

    Mirrors the ``_InMemoryCredentialStore`` idiom from ORA-30 — it is a *port*
    the service constructs against, not the production Postgres-backed store.
    All reads are organisation-scoped: a cross-org read returns ``None`` even
    when the token id matches (defence-in-depth above ReBAC).
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


# --- mint -------------------------------------------------------------------


async def test_mint_returns_raw_token_and_record(service, store) -> None:
    """AC1: minting returns ``(raw_token, record)`` and persists the record."""
    raw, record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    assert isinstance(raw, str) and raw
    assert record.organisation_id == _ORG
    assert record.member_id == _MEMBER
    assert record.agent_id == _AGENT
    assert frozenset(record.scopes) == _DELEGATED_SCOPES
    assert len(store.rows) == 1


async def test_mint_raw_token_is_not_persisted_in_clear(service, store) -> None:
    """AC4: the raw bearer bytes never appear on the persisted row in clear."""
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    (row,) = store.rows
    assert raw != row.token_hash
    assert raw not in row.token_hash
    # And the raw bytes are not equal to any other persisted scalar.
    for value in (row.token_prefix, str(row.id), str(row.member_id), str(row.agent_id)):
        assert raw != value


async def test_mint_returns_disjoint_raw_tokens_per_call(service) -> None:
    """Two mints for the same ``(member, agent)`` yield different raw tokens."""
    raw_a, _ = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )
    raw_b, _ = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )
    assert raw_a != raw_b


async def test_mint_rejects_empty_scopes(service) -> None:
    """A delegated token with no scopes carries no authority — fail closed.

    Without scopes there is no subset to validate against; the service must
    refuse the mint rather than persist a row that would silently authorise
    nothing (or, worse, be interpreted as "all scopes" by a buggy consumer).
    """
    with pytest.raises(ValueError):
        await service.mint(
            organisation_id=_ORG,
            member_id=_MEMBER,
            agent_id=_AGENT,
            scopes=frozenset(),
            expires_at=_utc_now() + timedelta(hours=1),
        )


async def test_mint_rejects_past_expiry(service) -> None:
    """A token minted with ``expires_at`` already in the past is rejected.

    Minting a pre-expired token would only be a footgun for the caller; the
    broker refuses at mint time rather than persist a dead row.
    """
    with pytest.raises(ValueError):
        await service.mint(
            organisation_id=_ORG,
            member_id=_MEMBER,
            agent_id=_AGENT,
            scopes=_DELEGATED_SCOPES,
            expires_at=_utc_now() - timedelta(seconds=1),
        )


# --- validate: happy path ---------------------------------------------------


async def test_validate_accepts_matching_agent_with_subset_scopes(service) -> None:
    """AC2 happy path: matching agent + non-expired + subset scopes → success."""
    raw, record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    assert result.success is True
    assert result.reason is None
    assert result.token_id == record.id
    assert result.member_id == _MEMBER
    assert result.agent_id == _AGENT
    assert frozenset(result.granted_scopes) == _DELEGATED_SCOPES


async def test_validate_accepts_equal_scope_set(service) -> None:
    """Requesting *exactly* the delegated set is not creep — it is the boundary."""
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=_DELEGATED_SCOPES,
    )

    assert result.success is True


# --- validate: failure modes ------------------------------------------------


async def test_validate_rejects_agent_mismatch(service) -> None:
    """AC2 (agent binding): a different agent presenting the token is rejected.

    Even if the requested scopes are within the delegated subset and the token
    is unexpired, a caller whose authenticated principal does not match the
    token's bound agent is denied. This is the primary T2 mitigation: a leaked
    delegated token is useless to any agent other than the one it was bound to.
    """
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_OTHER_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    assert result.success is False
    assert result.reason == "agent_mismatch"


async def test_validate_rejects_expired_token(service) -> None:
    """AC2 (expiry): a token whose ``expires_at`` is in the past is rejected.

    Expiry is checked at *every* use, not just at mint time — the broker is the
    enforcement point because the runtime cannot trust the agent's clock.
    """
    raw, record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(milliseconds=10),
    )

    # Simulate elapsed time by mutating the persisted expiry (test-double seam).
    record.expires_at = _utc_now() - timedelta(seconds=1)

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    assert result.success is False
    assert result.reason == "expired"


async def test_validate_rejects_scope_creep_extra_scope(service) -> None:
    """AC3 / T2 core: requesting *any* scope outside the delegated subset is rejected.

    This is the test that proves the broker enforces the scope cap. The token
    grants ``{drive.read, calendar.read}``; the agent attempts to perform an
    action requiring ``drive.write`` (a scope the member never delegated). The
    broker must reject — even though the agent matches, the token is unexpired,
    and ``drive.read`` is *also* in the requested set.
    """
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read", "drive.write"}),
    )

    assert result.success is False
    assert result.reason == "scope_creep"


async def test_validate_rejects_scope_creep_entirely_disjoint(service) -> None:
    """Disjoint requested scopes are still ``scope_creep`` — distinct from ``unknown``."""
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"admin.write"}),
    )

    assert result.success is False
    assert result.reason == "scope_creep"


async def test_validate_rejects_revoked_token(service) -> None:
    """A token revoked between mint and use is denied at validation."""
    raw, record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    revoked = await service.revoke(token_id=record.id, organisation_id=_ORG)
    assert revoked == 1

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    assert result.success is False
    assert result.reason == "revoked"


async def test_validate_rejects_unknown_token(service) -> None:
    """A well-formed but never-issued token validates to a rejection."""
    result = await service.validate(
        raw_token="odt_neverwasanytoken",  # noqa: S106 — synthetic test value, not a real bearer
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    assert result.success is False
    assert result.reason == "unknown"


# --- WP-11: constant-time token-hash comparison -----------------------------


async def test_validate_uses_constant_time_hash_compare(monkeypatch) -> None:  # noqa: ANN001
    """WP-11: the stored-vs-presented token-hash equality goes through
    ``hmac.compare_digest`` (timing-safe), not a plain ``!=``.

    A plain hex-digest comparison short-circuits on the first differing character, leaking — via
    response timing — how many leading characters a guessed bearer shares with the stored hash.
    This test pins that the equality check is routed through ``hmac.compare_digest`` by spying on
    it, and that behaviour is unchanged (the correct bearer still validates)."""
    import hmac as _hmac

    from oraclous_credential_broker_service.services import delegation_service as _mod

    store = _InMemoryDelegatedTokenStore()
    service = _mod.DelegationService(store=store)
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    calls: list[tuple[str, str]] = []
    real_compare = _hmac.compare_digest

    def _spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(_mod.hmac, "compare_digest", _spy)

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    assert result.success is True  # behaviour unchanged: the correct bearer validates
    assert calls, "token-hash equality must go through hmac.compare_digest (timing-safe)"


async def test_validate_accepts_correct_hash_rejects_wrong_hash(service) -> None:
    """WP-11 behaviour parity: the constant-time compare still ACCEPTS the correct bearer and
    REJECTS a tampered one that shares the lookup prefix (a same-prefix body flip → ``unknown``)."""
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    # Correct bearer → accepted.
    good = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )
    assert good.success is True

    # Same 12-char lookup prefix, different body → the hash differs → rejected as ``unknown`` (the
    # information-leak-safe default), never accepted.
    tampered = raw[:12] + ("X" if raw[12:13] != "X" else "Y") + raw[13:]
    assert tampered[:12] == raw[:12] and tampered != raw
    bad = await service.validate(
        raw_token=tampered,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )
    assert bad.success is False
    assert bad.reason == "unknown"


# --- revocation -------------------------------------------------------------


async def test_revoke_returns_count_then_zero_when_called_again(service) -> None:
    """Revocation is idempotent: second revoke reports zero rows changed."""
    _raw, record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    assert await service.revoke(token_id=record.id, organisation_id=_ORG) == 1
    assert await service.revoke(token_id=record.id, organisation_id=_ORG) == 0


# --- internal-only result shape (AC4 surface) -------------------------------


async def test_validation_result_does_not_carry_raw_token(service) -> None:
    """AC4: the validation result echoes binding metadata, never the raw bearer.

    Tokens are internal to the broker. The validation result hands the caller
    enough to authorise the action — member id, agent id, granted scopes — but
    never the raw bearer value, which would otherwise risk being forwarded to
    an external provider.
    """
    raw, _record = await service.mint(
        organisation_id=_ORG,
        member_id=_MEMBER,
        agent_id=_AGENT,
        scopes=_DELEGATED_SCOPES,
        expires_at=_utc_now() + timedelta(hours=1),
    )

    result = await service.validate(
        raw_token=raw,
        organisation_id=_ORG,
        requesting_agent_id=_AGENT,
        requested_scopes=frozenset({"drive.read"}),
    )

    for attr_value in vars(result).values():
        assert attr_value != raw, (
            "DelegationValidation must not echo the raw bearer token to callers"
        )
