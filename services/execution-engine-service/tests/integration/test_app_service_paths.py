"""App service paths that the tenancy tests do not reach (#932) — docker-required.

The isolation suite proves the widened read is safe. These cover the three things it never touches,
each raised at review as having no test at any layer:

* the slug lookup, which is how the console finds an Oraclous-provided app now that the frontend no
  longer carries its uuid in an environment variable;
* the refusal of a credentials mode that is declared but not built;
* the page-size clamp, which is the only thing standing between a caller and asking for every app
  in one request.

Real Postgres and the real repository throughout — a fake would agree with whatever the service
does, and two of these three are about what the service refuses.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType

pytestmark = [pytest.mark.integration]

PLATFORM_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _principal(org: uuid.UUID | None) -> Principal:
    return Principal(principal_id=USER_A, principal_type=PrincipalType.USER, organisation_id=org)


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
async def app_service(engine_dsns) -> AsyncIterator[Any]:  # noqa: ANN001
    """The real service on the real repository. ``team_runs`` is a bare stub: none of these paths
    starts a run, and wiring a real one would drag the harness in for no assertion."""
    from oraclous_execution_engine_service.repositories.app_repository import AppRepository
    from oraclous_execution_engine_service.services.app_service import AppService

    _owner_async_dsn, app_async_dsn = engine_dsns
    repo = AppRepository(app_async_dsn, platform_org_id=PLATFORM_ORG)
    try:
        yield AppService(
            apps=repo,
            team_runs=object(),  # type: ignore[arg-type]
            team_run_repository=object(),  # type: ignore[arg-type]
            platform_org_id=PLATFORM_ORG,
        )
    finally:
        await repo.close()


async def _seed(service: Any, *, slug: str, name: str) -> Any:
    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(PLATFORM_ORG):
        return await service._apps.upsert_platform_app(
            slug=slug,
            name=name,
            description=None,
            user_id=USER_A,
            manifest=_team(name),
            sub_harnesses={},
        )


async def test_a_tenant_finds_a_platform_app_by_its_slug(app_service: Any) -> None:
    """The deep link. The console asks for ``validation-desk`` rather than a uuid nobody can guess,
    and an organisation that was never seeded still resolves it."""
    seeded = await _seed(app_service, slug="validation-desk", name="Validation Desk")

    found = await app_service.get_by_slug("validation-desk", _principal(ORG_A))

    assert found["id"] == seeded.id
    assert found["origin"] == "platform"
    assert found["inputs"][0]["key"] == "task"


async def test_an_unknown_slug_is_a_404(app_service: Any) -> None:
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    with pytest.raises(TeamRunError) as excinfo:
        await app_service.get_by_slug("no-such-app", _principal(ORG_A))

    assert excinfo.value.status_code == 404


async def test_an_organisations_own_app_shadows_a_platform_app_of_the_same_slug(
    app_service: Any,
) -> None:
    """Both rows are visible under the widened read, so the lookup has to choose. It prefers the
    caller's own — an organisation that makes its own version of a default gets its own, and does
    not silently keep opening Oraclous's."""
    from oraclous_execution_engine_service.core.rls import org_scope

    await _seed(app_service, slug="validation-desk", name="Validation Desk")
    with org_scope(ORG_A):
        mine = await app_service._apps.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            name="Our own desk",
            description=None,
            slug="validation-desk",
            manifest=_team("ours"),
            sub_harnesses={},
        )

    found = await app_service.get_by_slug("validation-desk", _principal(ORG_A))

    assert found["id"] == mine.id
    assert found["origin"] == "organisation"


async def test_a_principal_with_no_organisation_is_refused(app_service: Any) -> None:
    """Fail-closed tenancy (ADR-006). A request that reached here with no organisation bound must
    be refused outright, not served the platform apps because those happen to be readable."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    await _seed(app_service, slug="validation-desk", name="Validation Desk")

    with pytest.raises(TeamRunError) as excinfo:
        await app_service.list_for_org(_principal(None))

    assert excinfo.value.status_code == 403


async def test_an_unsupported_credentials_mode_is_refused_before_anything_runs(
    app_service: Any,
) -> None:
    """``owner`` — the app's owner pays — is a declared value with no implementation behind it. It
    has to be refused with a reason, not quietly treated as ``caller``, which would spend the wrong
    person's money."""
    from oraclous_execution_engine_service.core.rls import org_scope
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    with org_scope(ORG_A):
        owner_paid = await app_service._apps.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            name="Owner pays",
            description=None,
            slug=None,
            manifest=_team("owner-paid"),
            sub_harnesses={},
            credentials_mode="owner",
        )

    with pytest.raises(TeamRunError) as excinfo:
        await app_service.run(
            owner_paid.id,
            _principal(ORG_A),
            inputs={"task": "x"},
            models=[
                {
                    "role": "primary",
                    "binding": "openrouter/x",
                    "protocol_shape": "openai-compatible",
                    "config": {"credential_id": str(uuid.uuid4())},
                }
            ],
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.error_type == "credentials_mode_unsupported"


async def test_the_page_size_is_clamped_however_the_caller_asks(app_service: Any) -> None:
    """Born bounded. A caller asking for everything gets a page, and one asking for nothing still
    gets a row rather than an empty list that looks like an empty tab."""
    from oraclous_execution_engine_service.core.rls import org_scope

    await _seed(app_service, slug="validation-desk", name="Validation Desk")
    with org_scope(ORG_A):
        for i in range(3):
            await app_service._apps.create(
                organisation_id=ORG_A,
                user_id=USER_A,
                name=f"App {i}",
                description=None,
                slug=None,
                manifest=_team(f"a-{i}"),
                sub_harnesses={},
            )

    huge, total = await app_service.list_for_org(_principal(ORG_A), limit=100_000)
    assert total == 4
    assert len(huge) == 4  # clamped to 200, which is above what exists here

    tiny, _ = await app_service.list_for_org(_principal(ORG_A), limit=0)
    assert len(tiny) == 1, "limit 0 clamps to 1, never to an empty page"

    negative, _ = await app_service.list_for_org(_principal(ORG_A), limit=2, offset=-5)
    assert len(negative) == 2, "a negative offset clamps to the first page"


async def test_every_listed_row_says_where_it_came_from(app_service: Any) -> None:
    """The console splits the tab on this field, so a row without it is a row it cannot place."""
    from oraclous_execution_engine_service.core.rls import org_scope

    await _seed(app_service, slug="validation-desk", name="Validation Desk")
    with org_scope(ORG_A):
        await app_service._apps.create(
            organisation_id=ORG_A,
            user_id=USER_A,
            name="Ours",
            description=None,
            slug=None,
            manifest=_team("ours"),
            sub_harnesses={},
        )

    rows, _ = await app_service.list_for_org(_principal(ORG_A))

    assert {r["origin"] for r in rows} == {"platform", "organisation"}
    assert all(r["origin"] in ("platform", "organisation") for r in rows)


async def test_a_cross_organisation_app_id_is_absent_not_forbidden(app_service: Any) -> None:
    """A 403 would confirm that some other organisation owns an app by this id. Absence tells a
    prober nothing (ADR-006, the team-draft posture)."""
    from oraclous_execution_engine_service.core.rls import org_scope
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    with org_scope(ORG_B):
        theirs = await app_service._apps.create(
            organisation_id=ORG_B,
            user_id=USER_A,
            name="B's app",
            description=None,
            slug=None,
            manifest=_team("b"),
            sub_harnesses={},
        )

    with pytest.raises(TeamRunError) as excinfo:
        await app_service.get(theirs.id, _principal(ORG_A))

    assert excinfo.value.status_code == 404
