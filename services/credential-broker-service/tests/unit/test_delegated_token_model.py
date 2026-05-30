"""Failing tests for the ``DelegatedToken`` storage model (ORA-32 / R1-B1).

The delegated-token primitive lets a member mint a short-lived, scope-limited
token bound to a specific agent. The broker persists the binding and validates
it per-use. This file pins the *storage shape* alone — the binding fields, the
outermost tenancy scope, and the absence of any raw-secret column on the
persisted row. Service-layer behaviour (mint, validate, scope-creep rejection,
revocation) lives in ``test_delegation_service.py``.

Threat reference: Structured Threat Catalogue **T2** — a delegated token bound
to ``(member, agent, scopes, expiry)`` is the broker-side defence against agent
scope creep. ADR-006: ``organisation_id`` is the outermost tenancy scope on
every substrate primitive.

Behavioural reference (Reshape): legacy
``credential-broker-service/app/models/credential_model.py`` for the storage-row
idiom (UUID pk, ``organisation_id`` outermost) — the delegated-token primitive
itself is new (no legacy precursor).

RED until ``backend-implementer`` creates
``oraclous_credential_broker_service.models.delegated_token``.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.organization_isolation]


def test_delegated_token_table_exists() -> None:
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    assert DelegatedToken.__tablename__ == "delegated_tokens"


def test_delegated_token_has_non_nullable_uuid_organisation_id() -> None:
    """ADR-006: ``organisation_id`` is the outermost tenancy scope, NOT NULL, UUID."""
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    columns = DelegatedToken.__table__.columns
    assert "organisation_id" in columns
    org = columns["organisation_id"]
    assert org.nullable is False, "organisation_id must be NOT NULL (ADR-006)"
    assert "UUID" in str(org.type).upper(), "organisation_id must be a UUID column"


def test_delegated_token_has_non_nullable_uuid_primary_key() -> None:
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    columns = DelegatedToken.__table__.columns
    assert "id" in columns
    pk = columns["id"]
    assert pk.primary_key is True
    assert "UUID" in str(pk.type).upper()


def test_delegated_token_binds_member_and_agent() -> None:
    """The binding fields ``member_id`` and ``agent_id`` are NOT NULL UUIDs.

    Per-use validation rejects any caller whose authenticated principal does
    not match the bound ``agent_id``; the bound ``member_id`` is what the
    broker treats as the delegating principal for audit purposes.
    """
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    columns = DelegatedToken.__table__.columns

    assert "member_id" in columns
    member = columns["member_id"]
    assert member.nullable is False
    assert "UUID" in str(member.type).upper()

    assert "agent_id" in columns
    agent = columns["agent_id"]
    assert agent.nullable is False
    assert "UUID" in str(agent.type).upper()


def test_delegated_token_carries_scopes_column() -> None:
    """The delegated scope subset is persisted on the token row.

    The implementer chooses the storage idiom (Postgres ``ARRAY`` or ``JSONB``);
    the test only pins that *some* scopes column exists and is NOT NULL — without
    scopes there is nothing to scope-creep-check against.
    """
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    columns = DelegatedToken.__table__.columns
    assert "scopes" in columns, "delegated_tokens must persist the granted scopes"
    assert columns["scopes"].nullable is False


def test_delegated_token_has_expires_at() -> None:
    """Per-use expiry enforcement requires a persisted ``expires_at``."""
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    columns = DelegatedToken.__table__.columns
    assert "expires_at" in columns
    expires = columns["expires_at"]
    assert expires.nullable is False
    assert "TIMESTAMP" in str(expires.type).upper() or "DATETIME" in str(expires.type).upper()


def test_delegated_token_has_status_column() -> None:
    """Revocation is modelled as a status flip, not a row delete (audit invariant)."""
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    columns = DelegatedToken.__table__.columns
    assert "status" in columns
    assert columns["status"].nullable is False


def test_delegated_token_does_not_store_raw_secret() -> None:
    """Internal-only invariant (ORA-32 AC4): no column holds the raw bearer value.

    The broker may persist an opaque ``token_hash`` / ``token_prefix`` index for
    lookup, but never the raw bytes. The mint surface returns the raw value
    *once*; nothing on the persisted row equals the raw value.
    """
    from oraclous_credential_broker_service.models.delegated_token import DelegatedToken

    column_names = {c.name.lower() for c in DelegatedToken.__table__.columns}
    forbidden = {"raw_token", "token", "token_value", "secret", "plaintext"}
    leaked = column_names & forbidden
    assert leaked == set(), (
        f"delegated_tokens must not persist the raw bearer value — found: {sorted(leaked)}"
    )
