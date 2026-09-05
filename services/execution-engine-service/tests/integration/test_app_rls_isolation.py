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
    from oraclous_substrate.access import org_scope

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

    from oraclous_substrate.access import org_scope

    with org_scope(ORG_A):
        rows_a, total_a = await app_repository.list_for_org(ORG_A)
    with org_scope(ORG_B):
        rows_b, total_b = await app_repository.list_for_org(ORG_B)

    assert total_a == 1 and total_b == 1
    assert rows_a[0]["id"] == seeded.id == rows_b[0]["id"]
    assert rows_a[0]["organisation_id"] == PLATFORM_ORG


async def test_a_tenants_own_app_is_invisible_to_another_tenant(app_repository: Any) -> None:
    """Widening the read must not have widened it to everything. Org A's own app stays A's."""
    from oraclous_substrate.access import org_scope

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

    from oraclous_substrate.access import org_scope

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
    from oraclous_substrate.access import org_scope
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

    assert "42501" in str(excinfo.value)  # InsufficientPrivilege, not a silent success


async def test_a_tenant_cannot_edit_or_delete_the_platform_app(app_repository: Any) -> None:
    """Reading a shared row must not imply owning it. A tenant that could rename or delete the
    Validation Desk would be editing every other organisation's tab."""
    from oraclous_substrate.access import org_scope

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


async def test_an_unbound_organisation_reads_nothing(app_repository: Any) -> None:
    """Fail closed. With no organisation bound the GUC is empty, and the widened read must not be a
    way in — a request that forgot to bind its org sees zero rows, including the platform ones."""
    await _seed_platform_app(app_repository, name="Validation Desk", slug="validation-desk")

    rows, total = await app_repository.list_for_org(ORG_A)  # no org_scope around it

    assert rows == [] and total == 0


async def test_the_list_row_never_carries_the_documents(app_repository: Any) -> None:
    """A tab listing must not load a five-member manifest per tile. The member count is dug out of
    the manifest at query time instead, exactly as the team-draft list does."""
    await _seed_platform_app(app_repository, name="Validation Desk", slug="validation-desk")

    from oraclous_substrate.access import org_scope

    with org_scope(ORG_A):
        rows, _ = await app_repository.list_for_org(ORG_A)

    assert "manifest" not in rows[0]
    assert "sub_harnesses" not in rows[0]
    assert rows[0]["member_count"] == 1


async def test_the_total_agrees_with_the_page_under_the_widened_read(app_repository: Any) -> None:
    """The count query must carry the SAME widened predicate as the page query. If it does not, a
    tab paginates against a total that never counted the platform apps."""
    await _seed_platform_app(app_repository, name="Validation Desk", slug="validation-desk")

    from oraclous_substrate.access import org_scope

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
