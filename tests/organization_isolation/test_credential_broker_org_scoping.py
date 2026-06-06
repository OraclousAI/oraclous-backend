"""Organisation- AND user-scoped credential storage (ORA-33, R1-B2; hardened by the
credential-broker defense-in-depth follow-up).

The repository scopes every read and write by ``organisation_id`` AND ``user_id`` as
defense-in-depth: a leaked/guessed credential id cannot cross an organisation boundary (Structured
Threat Catalogue T6, ADR-008), nor be read/updated/deleted by another user within the same org. The
trusted runtime resolver passes ``user_id=None`` to scope by org only (service→service, acting for
whatever user is executing); the user-facing surface always passes the principal's user id.

Proven at the data layer against a real Postgres (ORA-12 harness), per the no-mocks-for-the-database
rule. ``organisation_id`` + ``user_id`` are explicit repository arguments resolved from the
authenticated context — never from a request body (ORG001). Per-org KMS key material is out of
scope (deferred to R8).
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
async def broker_repo(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CredentialRepository]:
    # The repo now encrypts on write (AES-256-GCM), so the Settings secrets must be present.
    monkeypatch.setenv("ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")  # noqa: S105
    monkeypatch.setenv("DATABASE_URL", _asyncpg_url(postgres_dsn))
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "test-internal-key")  # noqa: S105
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()
    repo = CredentialRepository(_asyncpg_url(postgres_dsn))
    await repo.create_tables()
    try:
        yield repo
    finally:
        await repo.close()
        get_settings.cache_clear()


async def test_create_persists_organisation_id(broker_repo: CredentialRepository) -> None:
    org = uuid.uuid4()
    user = uuid.uuid4()
    cred = await broker_repo.create_credential(
        _make_create(user_id=user), organisation_id=org, user_id=user
    )
    assert cred.organisation_id == org


async def test_get_by_id_within_organisation_returns_credential(
    broker_repo: CredentialRepository,
) -> None:
    org = uuid.uuid4()
    user = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=user), organisation_id=org, user_id=user
    )
    # org-only read (the trusted-runtime scope) finds it
    fetched = await broker_repo.get_credential_by_id(created.id, organisation_id=org)
    assert fetched is not None
    assert fetched.id == created.id


async def test_cross_organisation_get_by_id_is_denied(
    broker_repo: CredentialRepository,
) -> None:
    """The core T6 assertion: a credential id is not readable from another org."""
    owning_org = uuid.uuid4()
    other_org = uuid.uuid4()
    user = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=user), organisation_id=owning_org, user_id=user
    )

    leaked = await broker_repo.get_credential_by_id(created.id, organisation_id=other_org)
    assert leaked is None, "a credential must not be readable from another organisation"


async def test_cross_user_same_org_is_denied(broker_repo: CredentialRepository) -> None:
    """User-scoping: a credential is not readable/updatable/deletable by another user in the org."""
    org = uuid.uuid4()
    owner = uuid.uuid4()
    intruder = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=owner), organisation_id=org, user_id=owner
    )

    # another user in the SAME org cannot read, delete, or update it
    assert (
        await broker_repo.get_credential_by_id(created.id, organisation_id=org, user_id=intruder)
    ) is None
    assert (
        await broker_repo.delete_credential(created.id, organisation_id=org, user_id=intruder)
    ) is False
    hijack = await broker_repo.update_credential(
        CredentialsUpdate(
            id=created.id,
            name="hijacked",
            provider="github",
            user_id=intruder,
            tool_id=created.tool_id,
            cred_type="api_key",
            credential={"access_token": "tok-xyz"},
        ),
        organisation_id=org,
        user_id=intruder,
    )
    assert hijack is None

    # the owner still sees it, unchanged
    survivor = await broker_repo.get_credential_by_id(
        created.id, organisation_id=org, user_id=owner
    )
    assert survivor is not None and survivor.name == "primary"


async def test_list_credentials_scoped_to_organisation(
    broker_repo: CredentialRepository,
) -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    user = uuid.uuid4()
    a_cred = await broker_repo.create_credential(
        _make_create(user_id=user), organisation_id=org_a, user_id=user
    )
    await broker_repo.create_credential(
        _make_create(user_id=user), organisation_id=org_b, user_id=user
    )

    listed = await broker_repo.list_credentials(
        RequestCredentials(user_id=user), organisation_id=org_a
    )
    assert [c.id for c in listed] == [a_cred.id]


async def test_cross_organisation_delete_is_denied(
    broker_repo: CredentialRepository,
) -> None:
    owning_org = uuid.uuid4()
    other_org = uuid.uuid4()
    user = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=user), organisation_id=owning_org, user_id=user
    )

    deleted = await broker_repo.delete_credential(
        created.id, organisation_id=other_org, user_id=user
    )
    assert deleted is False
    # the owning organisation's credential survives a cross-org delete attempt
    survivor = await broker_repo.get_credential_by_id(created.id, organisation_id=owning_org)
    assert survivor is not None


async def test_cross_organisation_update_is_denied(
    broker_repo: CredentialRepository,
) -> None:
    owning_org = uuid.uuid4()
    other_org = uuid.uuid4()
    user = uuid.uuid4()
    created = await broker_repo.create_credential(
        _make_create(user_id=user, name="original"), organisation_id=owning_org, user_id=user
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
        user_id=user,
    )

    unchanged = await broker_repo.get_credential_by_id(created.id, organisation_id=owning_org)
    assert unchanged is not None
    assert unchanged.name == "original"


async def test_same_organisation_crud_roundtrip(broker_repo: CredentialRepository) -> None:
    """AC3: existing CRUD still works end-to-end within a single organisation + user."""
    org = uuid.uuid4()
    user = uuid.uuid4()

    created = await broker_repo.create_credential(
        _make_create(user_id=user, name="original"), organisation_id=org, user_id=user
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
        user_id=user,
    )
    assert updated is not None
    assert updated.name == "renamed"

    deleted = await broker_repo.delete_credential(created.id, organisation_id=org, user_id=user)
    assert deleted is True
    assert await broker_repo.get_credential_by_id(created.id, organisation_id=org) is None
