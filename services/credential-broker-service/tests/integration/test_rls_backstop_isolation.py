"""Data-layer proof that the Postgres RLS backstop isolates the credential-broker's org-scoped
tables on its own — the app-layer ``WHERE organisation_id`` removed (ADR-030 Slice 0).

Distinct from ``test_credential_api`` (which proves the API + app-layer scoping behave): here we go
under the API and prove the *backstop* — that even with the app-layer predicate gone, RLS alone
returns only the bound org's rows and denies a cross-org write. This is the real point of the
backstop: a bug that drops a ``WHERE`` clause no longer leaks cross-org rows.

Run as the NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role (the deployed runtime role) — RLS only
bites a non-superuser, so the isolation proven here is real. The org GUC is bound exactly as the
service binds it (``bind_org_guc_async`` / the engine ``begin`` guard), never via a hand-written
WHERE. Mirrors the substrate ``test_org_guc_isolates_postgres_reads_and_writes`` for broker tables.

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §2; ADR-030.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# Direct SQL against the table with NO organisation_id predicate — RLS is the only thing scoping it.
_SELECT_ALL_NAMES = "SELECT name FROM user_credentials"
_COUNT_ALL = "SELECT count(*) FROM user_credentials"
_INSERT = (
    "INSERT INTO user_credentials "
    "(id, organisation_id, name, provider, user_id, tool_id, encrypted_cred) "
    "VALUES (:id, :org, :name, 'p', :user, :tool, 'ct')"
)


@pytest.fixture
async def app_engine(broker_dsns) -> AsyncIterator[AsyncEngine]:
    """An AsyncEngine on the oraclous_app role with the org-GUC guard installed (so a transaction
    bound via ``org_scope`` / ``use_organisation_context`` sets the GUC), schema+RLS+role already
    provisioned by ``broker_dsns`` as the owner."""
    from oraclous_substrate.access_async import install_org_guc_guard

    _owner_async_dsn, app_async_dsn = broker_dsns
    engine = create_async_engine(app_async_dsn)
    install_org_guc_guard(engine)
    yield engine
    await engine.dispose()


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


async def test_rls_alone_isolates_reads_and_denies_cross_org_writes(
    app_engine: AsyncEngine,
) -> None:
    from oraclous_governance import use_organisation_context

    # WRITE org A's row with org A bound (the engine begin-guard sets the GUC; WITH CHECK admits it)
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            await conn.execute(
                text(_INSERT),
                {
                    "id": uuid.uuid4(),
                    "org": ORG_A,
                    "name": "org-a-cred",
                    "user": uuid.uuid4(),
                    "tool": uuid.uuid4(),
                },
            )

    # READ under org A's GUC: the row is visible — and the SELECT carries NO organisation_id WHERE,
    # so RLS alone is what scopes it.
    with use_organisation_context(_ctx(ORG_A)):
        async with app_engine.begin() as conn:
            a_rows = [r[0] for r in (await conn.execute(text(_SELECT_ALL_NAMES))).all()]
    assert a_rows == ["org-a-cred"]

    # READ under org B's GUC: org A's row is INVISIBLE (RLS USING filters it) — the backstop proof
    # with the app-WHERE removed.
    with use_organisation_context(_ctx(ORG_B)):
        async with app_engine.begin() as conn:
            b_rows = [r[0] for r in (await conn.execute(text(_SELECT_ALL_NAMES))).all()]
    assert b_rows == []

    # CROSS-ORG WRITE: with org B bound, inserting a row stamped for org A violates the RLS WITH
    # CHECK → SQLSTATE 42501 (InsufficientPrivilege). org B cannot write into org A's scope.
    with pytest.raises(ProgrammingError) as exc_info:
        with use_organisation_context(_ctx(ORG_B)):
            async with app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT),
                    {
                        "id": uuid.uuid4(),
                        "org": ORG_A,  # smuggled — not the bound org
                        "name": "smuggled",
                        "user": uuid.uuid4(),
                        "tool": uuid.uuid4(),
                    },
                )
    assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"

    # FAIL-CLOSED: with NO org context bound, the guard binds the empty GUC → RLS exposes zero rows
    # (an absent scope denies, never defaults — T1-M1). org A's real row stays hidden.
    async with app_engine.begin() as conn:
        assert (await conn.execute(text(_COUNT_ALL))).scalar_one() == 0


async def test_runtime_role_is_non_bypassing(app_engine: AsyncEngine) -> None:
    """The role the runtime connects as must be NOSUPERUSER/NOBYPASSRLS, else the policy is inert
    (T1-M3) — the precondition the isolation above depends on, and what
    ``assert_runtime_role_isolates`` enforces at startup."""
    from oraclous_substrate.access_async import assert_non_bypassing_role

    # passes silently for oraclous_app; would raise RlsBypassingRoleError for a superuser.
    await assert_non_bypassing_role(app_engine)

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).first()
    assert row is not None
    assert row[0] is False and row[1] is False  # NOSUPERUSER, NOBYPASSRLS
