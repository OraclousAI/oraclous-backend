"""Failing tests for organisation-scoped credential storage (ORA-33, R1-B2).

Behavioural reference: legacy
``credential-broker-service/app/repositories/credential_repository.py``, whose
``get_credential_by_id`` filters by ``id`` alone — any organisation's credential
id is readable. The Reshape adds ``organisation_id`` scoping to every read and
write as defense-in-depth (the authenticated principal already binds the user;
this stops a leaked/guessed credential id from crossing an organisation
boundary).

Proven at the data layer against a real Postgres (ORA-12 harness), per the
project's no-mocks-for-the-database rule. Pins the second/third acceptance
criteria, Structured Threat Catalogue T6 (operator separation, ADR-008) and
ADR-006 (organisation_id on every operation).

``organisation_id`` is supplied as an explicit repository argument resolved from
the authenticated context — never from a request body (ORG001 guardrail /
ORA-40 security-architect ruling). Per-org / customer KMS key material is out of
scope (deferred to R8).

RED until ``backend-implementer`` reshapes
``oraclous_credential_broker_service.repositories.credential_repository`` and its
schema/model to carry ``organisation_id``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from oraclous_credential_broker_service.repositories.credential_repository import (
    CredentialRepository,
)
from oraclous_credential_broker_service.schema.credential_schema import (
    CreateCredential,
    CredentialsUpdate,
    RequestCredentials,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.operator_separation,
    pytest.mark.security,
]


def _asyncpg_url(dsn: str) -> str:
    """Adapt the libpq DSN from the harness to the async driver the repo uses."""
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _make_create(*, user_id: uuid.UUID, name: str = "primary") -> CreateCredential:
    return CreateCredential(
        tool_id=uuid.uuid4(),
        user_id=user_id,
        name=name,
        provider="github",
        cred_type="api_key",
        credential={"access_token": "tok-abc"},
    )


@pytest.fixture
async def broker_repo(postgres_dsn: str) -> AsyncIterator[CredentialRepository]:
    repo = CredentialRepository(_asyncpg_url(postgres_dsn))
    await repo.create_tables()
    try:
        yield repo
    finally:
        await repo.close()


async def test_create_persists_organisation_id(broker_repo: CredentialRepository) -> None:
    org = uuid.uuid4()
    cred = await broker_repo.create_credential(
        _make_create(user_id=uuid.uuid4()), organisation_id=org
    )
    assert cred.organisation_id == org


async def test_get_by_id_within_organisation_returns_credential(
    broker_repo: CredentialRepository,
) -> None:
    org = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=uuid.uuid4()), organisation_id=org
    )
    fetched = await broker_repo.get_credential_by_id(created.id, organisation_id=org)
    assert fetched is not None
    assert fetched.id == created.id


async def test_cross_organisation_get_by_id_is_denied(
    broker_repo: CredentialRepository,
) -> None:
    """The core T6 assertion: a credential id is not readable from another org."""
    owning_org = uuid.uuid4()
    other_org = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=uuid.uuid4()), organisation_id=owning_org
    )

    leaked = await broker_repo.get_credential_by_id(created.id, organisation_id=other_org)
    assert leaked is None, "a credential must not be readable from another organisation"


async def test_list_credentials_scoped_to_organisation(
    broker_repo: CredentialRepository,
) -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    user = uuid.uuid4()
    a_cred = await broker_repo.create_credential(_make_create(user_id=user), organisation_id=org_a)
    await broker_repo.create_credential(_make_create(user_id=user), organisation_id=org_b)

    listed = await broker_repo.list_credentials(
        RequestCredentials(user_id=user), organisation_id=org_a
    )
    assert [c.id for c in listed] == [a_cred.id]


async def test_cross_organisation_delete_is_denied(
    broker_repo: CredentialRepository,
) -> None:
    owning_org = uuid.uuid4()
    other_org = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=uuid.uuid4()), organisation_id=owning_org
    )

    deleted = await broker_repo.delete_credential(created.id, organisation_id=other_org)
    assert deleted is False
    # the owning organisation's credential survives a cross-org delete attempt
    survivor = await broker_repo.get_credential_by_id(created.id, organisation_id=owning_org)
    assert survivor is not None


async def test_cross_organisation_update_is_denied(
    broker_repo: CredentialRepository,
) -> None:
    owning_org = uuid.uuid4()
    other_org = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=uuid.uuid4(), name="original"), organisation_id=owning_org
    )

    await broker_repo.update_credential(
        CredentialsUpdate(
            id=created.id,
            name="hijacked",
            provider="github",
            user_id=created.user_id,
            tool_id=created.tool_id,
            cred_type="api_key",
            credential={"access_token": "tok-xyz"},
        ),
        organisation_id=other_org,
    )

    unchanged = await broker_repo.get_credential_by_id(created.id, organisation_id=owning_org)
    assert unchanged is not None
    assert unchanged.name == "original"


async def test_same_organisation_crud_roundtrip(broker_repo: CredentialRepository) -> None:
    """AC3: existing CRUD still works end-to-end within a single organisation."""
    org = uuid.uuid4()
    user = uuid.uuid4()

    created = await broker_repo.create_credential(
        _make_create(user_id=user, name="original"), organisation_id=org
    )

    updated = await broker_repo.update_credential(
        CredentialsUpdate(
            id=created.id,
            name="renamed",
            provider="github",
            user_id=user,
            tool_id=created.tool_id,
            cred_type="api_key",
            credential={"access_token": "tok-abc"},
        ),
        organisation_id=org,
    )
    assert updated is not None
    assert updated.name == "renamed"

    deleted = await broker_repo.delete_credential(created.id, organisation_id=org)
    assert deleted is True
    assert await broker_repo.get_credential_by_id(created.id, organisation_id=org) is None
