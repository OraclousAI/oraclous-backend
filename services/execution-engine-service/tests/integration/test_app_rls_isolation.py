"""The apps store against a REAL Postgres + the RLS backstop (ADR-030) — docker-required.

``engine_apps`` is the second table in the whole repo to take a WIDENED read (after
``capability_descriptors``, which carries the built-in tool catalogue): the ``USING`` clause admits
the caller's organisation OR the platform organisation, while ``WITH CHECK`` stays the strict
caller-org equality. That asymmetry is the entire mechanism by which an Oraclous-provided app
reaches every organisation without anyone seeding a copy per tenant — and it is exactly the kind of
loosening that has to be proven rather than asserted, because a mistake here turns one shared row
into a hole between tenants.

Four things are proven HERE, on the NOSUPERUSER ``oraclous_app`` role so the policy actually bites
(a superuser would bypass it):

1. every organisation reads the SAME platform app row — one row, not a copy per tenant;
2. one tenant never sees another tenant's app;
3. a tenant cannot INSERT, UPDATE or DELETE a platform-org row (the catalogue-poison case);
4. an unbound organisation reads nothing.

RED until the app model, migration and repository land; the seams are imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

PLATFORM_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
PLATFORM_USER = uuid.UUID("00000000-0000-0000-0000-00000000000a")


def _team(name: str) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": name,
            "owner_organization_id": str(PLATFORM_ORG),
            "kind": "team",
        },
        "task_input": {"required": True, "key": "task", "description": "The idea to validate."},
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:desk/researcher@1",
                "subgoal": "gather evidence",
                "depends_on": [],
                "outputs_schema": {"required": ["summary"]},
            }
        ],
        "runtime": {"entrypoint": "researcher"},
    }


@pytest.fixture
async def app_repository(engine_dsns) -> AsyncIterator[Any]:  # noqa: ANN001
    """The REAL request-path repository on the org-bound ``oraclous_app`` engine, wired as
    deployment wires it (the GUC guard installed by default)."""
    from oraclous_execution_engine_service.repositories.app_repository import AppRepository

    _owner_async_dsn, app_async_dsn = engine_dsns
    repo = AppRepository(app_async_dsn, platform_org_id=PLATFORM_ORG)
    try:
        yield repo
    finally:
        await repo.close()


async def _seed_platform_app(repo: Any, *, name: str, slug: str) -> Any:
    """Write a platform-owned app the way the startup seed does — inside the platform org's own
    scope, because the strict WITH CHECK admits the INSERT only when the bound org matches the
    row's org. Without the scope this raises 42501, which is the point of the poison test below."""
    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(PLATFORM_ORG):
        return await repo.upsert_platform_app(
            slug=slug,
            name=name,
            description="Turns one idea into one decision brief.",
            user_id=PLATFORM_USER,
            manifest=_team(name),
            sub_harnesses={},
        )


async def test_every_organisation_reads_the_same_platform_app(app_repository: Any) -> None:
    """The whole point of the widened read. Two unrelated tenants, neither of which was seeded or
    provisioned, both list the Oraclous-provided app — and it is the SAME row id in both, so this
    is one shared record rather than a copy handed to each organisation."""
    seeded = await _seed_platform_app(
        app_repository, name="Validation Desk", slug="validation-desk"
    )

    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(ORG_A):
        rows_a, total_a = await app_repository.list_for_org(ORG_A)
    with org_scope(ORG_B):
        rows_b, total_b = await app_repository.list_for_org(ORG_B)

    assert total_a == 1 and total_b == 1
    assert rows_a[0]["id"] == seeded.id == rows_b[0]["id"]
    assert rows_a[0]["organisation_id"] == PLATFORM_ORG


async def test_a_tenants_own_app_is_invisible_to_another_tenant(app_repository: Any) -> None:
    """Widening the read must not have widened it to everything. Org A's own app stays A's."""
    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(ORG_A):
        await app_repository.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            name="A's pricing check",
            description=None,
            slug=None,
            manifest=_team("a-team"),
            sub_harnesses={},
        )

    with org_scope(ORG_B):
        rows, total = await app_repository.list_for_org(ORG_B)

    assert rows == [] and total == 0


async def test_a_tenant_sees_its_own_app_alongside_the_platform_one(app_repository: Any) -> None:
    """The Apps tab holds both kinds at once, from ONE list read — which is what lets the console
    render the two-kind split without a second endpoint."""
    await _seed_platform_app(app_repository, name="Validation Desk", slug="validation-desk")

    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(ORG_A):
        await app_repository.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            name="A's pricing check",
            description=None,
            slug=None,
            manifest=_team("a-team"),
            sub_harnesses={},
        )
        rows, total = await app_repository.list_for_org(ORG_A)

    assert total == 2
    owners = {r["organisation_id"] for r in rows}
    assert owners == {PLATFORM_ORG, ORG_A}


async def test_a_tenant_cannot_write_a_row_owned_by_the_platform_org(app_repository: Any) -> None:
    """The catalogue-poison case. A widened READ must never become a widened WRITE, or any tenant
    could plant an app that every other organisation then sees and runs. The strict WITH CHECK is
    what refuses it, at the database, under the runtime role."""
    from oraclous_execution_engine_service.core.rls import org_scope
    from sqlalchemy.exc import DBAPIError

    with org_scope(ORG_A), pytest.raises(DBAPIError) as excinfo:
        await app_repository.create(
            organisation_id=PLATFORM_ORG,  # A, pretending to be Oraclous
            user_id=USER_A,
            name="Definitely official",
            description=None,
            slug=None,
            manifest=_team("poison"),
            sub_harnesses={},
        )

    # The SQLSTATE lives on the driver's exception, NOT in the rendered message — that text reads
    # "new row violates row-level security policy for table ..." and never contains the number. An
    # earlier version of this test matched the string and so could only ever fail; the raw-SQL
    # sibling below reads `.orig.sqlstate` and is the correct shape.
    assert getattr(excinfo.value.orig, "sqlstate", None) == "42501"  # InsufficientPrivilege


async def test_a_tenant_cannot_edit_or_delete_the_platform_app(app_repository: Any) -> None:
    """Reading a shared row must not imply owning it. A tenant that could rename or delete the
    Validation Desk would be editing every other organisation's tab."""
    from oraclous_execution_engine_service.core.rls import org_scope

    seeded = await _seed_platform_app(
        app_repository, name="Validation Desk", slug="validation-desk"
    )

    with org_scope(ORG_A):
        renamed = await app_repository.rename(seeded.id, ORG_A, name="Mine now")
        deleted = await app_repository.delete(seeded.id, ORG_A)

    assert renamed is None  # the write predicate matches no row for this caller
    assert deleted is False

    with org_scope(ORG_B):
        rows, _ = await app_repository.list_for_org(ORG_B)
    assert rows[0]["name"] == "Validation Desk"  # untouched for everyone else


async def test_an_unbound_request_leaks_no_tenant_data(app_repository: Any) -> None:
    """Fail closed on the part that matters. With no organisation bound the GUC is empty, so the
    strict branch of the policy matches nothing and a tenant's rows are unreachable.

    The platform branch is a FIXED LITERAL and therefore GUC-independent, so an unbound read still
    admits the platform rows. That is not a leak — those rows are readable by every organisation by
    design — and asserting zero rows here would encode the opposite, then quietly pass anyway
    because the repository's own predicate would hide them. What must hold is the tenant half."""
    await _seed_platform_app(app_repository, name="Validation Desk", slug="validation-desk")

    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(ORG_A):
        await app_repository.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            name="A's private app",
            description=None,
            slug=None,
            manifest=_team("a-team"),
            sub_harnesses={},
        )

    rows, _total = await app_repository.list_for_org(ORG_A)  # no org_scope around it

    assert "A's private app" not in {r["name"] for r in rows}


async def test_the_list_row_never_carries_the_documents(app_repository: Any) -> None:
    """A tab listing must not load a five-member manifest per tile. The member count is dug out of
    the manifest at query time instead, exactly as the team-draft list does."""
    await _seed_platform_app(app_repository, name="Validation Desk", slug="validation-desk")

    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(ORG_A):
        rows, _ = await app_repository.list_for_org(ORG_A)

    assert "manifest" not in rows[0]
    assert "sub_harnesses" not in rows[0]
    assert rows[0]["member_count"] == 1


async def test_the_total_agrees_with_the_page_under_the_widened_read(app_repository: Any) -> None:
    """The count query must carry the SAME widened predicate as the page query. If it does not, a
    tab paginates against a total that never counted the platform apps."""
    await _seed_platform_app(app_repository, name="Validation Desk", slug="validation-desk")

    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(ORG_A):
        for i in range(3):
            await app_repository.create(
                organisation_id=ORG_A,
                user_id=USER_A,
                name=f"A's app {i}",
                description=None,
                slug=None,
                manifest=_team(f"a-{i}"),
                sub_harnesses={},
            )
        page, total = await app_repository.list_for_org(ORG_A, limit=2, offset=0)

    assert total == 4  # 3 of A's own + the platform one
    assert len(page) == 2


# --------------------------------------------------------------------------------------------
# The policy itself, in raw SQL.
#
# Everything above goes through the repository, so all of it would still pass if the repository
# reimplemented the platform union as its own WHERE clause and the table's policy were strict — or
# missing. That is the failure this section exists to catch: the widened read has to be in the
# DATABASE, because the policy is the backstop for every path that does not go through this
# repository. Direct SQL with NO organisation predicate; the policy is the only thing scoping it.
# Modelled on the capability-registry proof of the same shape.
# --------------------------------------------------------------------------------------------

_INSERT_APP = (
    "INSERT INTO engine_apps "
    "(id, organisation_id, user_id, name, manifest, sub_harnesses, manifest_fingerprint) "
    "VALUES (:id, :org, :user, :name, '{}'::jsonb, '{}'::jsonb, :fp)"
)
_SELECT_APP_NAMES = "SELECT name FROM engine_apps ORDER BY name"


@pytest.fixture
async def raw_app_engine(engine_dsns) -> AsyncIterator[Any]:  # noqa: ANN001
    """The org-bound engine on the NOSUPERUSER ``oraclous_app`` role with the same GUC guard the
    runtime installs — no repository in the way. A superuser engine here would bypass the policy
    and every assertion below would pass against a table with no policy at all."""
    from oraclous_substrate.access_async import install_org_guc_guard
    from sqlalchemy.ext.asyncio import create_async_engine

    _owner_async_dsn, app_async_dsn = engine_dsns
    engine = create_async_engine(app_async_dsn)
    install_org_guc_guard(engine)
    yield engine
    await engine.dispose()


def _ctx(org: uuid.UUID):  # noqa: ANN202
    from oraclous_governance import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=org, principal_id=uuid.uuid4(), principal_type=PrincipalType.USER
    )


async def test_the_policy_itself_admits_the_platform_org_and_no_other_tenant(
    raw_app_engine: Any,
) -> None:
    """The load-bearing read, proven at the database with no repository involved: each tenant reads
    its own rows plus the platform's, and never the other tenant's."""
    from oraclous_governance import use_organisation_context
    from sqlalchemy import text

    for org, name in ((PLATFORM_ORG, "platform-desk"), (ORG_A, "org-a-app"), (ORG_B, "org-b-app")):
        with use_organisation_context(_ctx(org)):
            async with raw_app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_APP),
                    {
                        "id": uuid.uuid4(),
                        "org": org,
                        "user": USER_A,
                        "name": name,
                        "fp": f"fp-{name}",
                    },
                )

    with use_organisation_context(_ctx(ORG_A)):
        async with raw_app_engine.begin() as conn:
            a_names = [r[0] for r in (await conn.execute(text(_SELECT_APP_NAMES))).all()]
    assert a_names == ["org-a-app", "platform-desk"]

    with use_organisation_context(_ctx(ORG_B)):
        async with raw_app_engine.begin() as conn:
            b_names = [r[0] for r in (await conn.execute(text(_SELECT_APP_NAMES))).all()]
    assert b_names == ["org-b-app", "platform-desk"]


async def test_the_policy_denies_a_tenant_writing_into_the_platform_org(
    raw_app_engine: Any,
) -> None:
    """The write side, at the database. A widened READ must never imply a widened WRITE, or any
    tenant could plant an app that every other organisation then sees and runs."""
    from oraclous_governance import use_organisation_context
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError) as excinfo:  # noqa: PT012 — the write is the assertion
        with use_organisation_context(_ctx(ORG_A)):
            async with raw_app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_APP),
                    {
                        "id": uuid.uuid4(),
                        "org": PLATFORM_ORG,
                        "user": USER_A,
                        "name": "catalogue-poison",
                        "fp": "fp-poison",
                    },
                )

    assert getattr(excinfo.value.orig, "sqlstate", None) == "42501"


async def test_an_unbound_read_reaches_no_tenant_row_at_the_database(raw_app_engine: Any) -> None:
    """With no organisation bound the GUC is empty and the strict branch matches nothing, so a
    tenant's rows are unreachable. The platform branch is a fixed literal and stays readable —
    which is correct, since those rows are public to every organisation by design."""
    from oraclous_governance import use_organisation_context
    from sqlalchemy import text

    for org, name in ((PLATFORM_ORG, "platform-desk"), (ORG_A, "org-a-app")):
        with use_organisation_context(_ctx(org)):
            async with raw_app_engine.begin() as conn:
                await conn.execute(
                    text(_INSERT_APP),
                    {
                        "id": uuid.uuid4(),
                        "org": org,
                        "user": USER_A,
                        "name": name,
                        "fp": f"fp-{name}",
                    },
                )

    async with raw_app_engine.begin() as conn:
        unscoped = [r[0] for r in (await conn.execute(text(_SELECT_APP_NAMES))).all()]

    assert unscoped == ["platform-desk"]
