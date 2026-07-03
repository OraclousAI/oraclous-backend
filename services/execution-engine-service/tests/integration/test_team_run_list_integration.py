"""TeamRunService.list_for_org against REAL Postgres + the RLS backstop (#633; docker-required).

The org-scoped team-run LIST end-to-end on the substrate it ships on: the REAL
``TeamRunRepository`` + ``TeamRunService`` on the NOSUPERUSER ``oraclous_app`` org-bound engine
(the GUC guard installed by default), wired as ``get_team_run_service`` wires it. Proven HERE: the
list returns the org's runs newest-first, the ``state`` filter and pagination bound the read,
``team_name`` is dug out of
``manifest.metadata.name``, ``total`` is the FULL matching count, and a second org sees ZERO — the
team_runs RLS policy scopes the read to the request-bound org, not just an app-layer WHERE.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.team_run_service import TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.integration

ORG_A = uuid.UUID("a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1")
ORG_B = uuid.UUID("b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2")
USER_A = uuid.UUID("1a1a1a1a-1a1a-1a1a-1a1a-1a1a1a1a1a1a")
USER_B = uuid.UUID("2b2b2b2b-2b2b-2b2b-2b2b-2b2b2b2b2b2b")


def _principal(org: uuid.UUID, user: uuid.UUID) -> Principal:
    return Principal(principal_id=user, principal_type=PrincipalType.USER, organisation_id=org)


def _manifest(name: str, org: uuid.UUID) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {"id": str(uuid.uuid4()), "name": name, "kind": "team"},
        "members": [{"role": "a", "kind": "agent"}],
    }


@pytest.fixture
async def repo(engine_dsns) -> AsyncIterator[TeamRunRepository]:  # noqa: ANN001
    _owner, app_async_dsn = engine_dsns
    r = TeamRunRepository(app_async_dsn)
    try:
        yield r
    finally:
        await r.close()


async def _seed(
    repo: TeamRunRepository, org: uuid.UUID, user: uuid.UUID, name: str, *, state: str | None = None
) -> uuid.UUID:
    """Create a QUEUED run for ``org`` (RLS-admitted under org_scope) and optionally transition it
    to a terminal/paused ``state`` — the fixture data for the list assertions."""
    with org_scope(org):
        row = await repo.create(
            organisation_id=org,
            user_id=user,
            manifest=_manifest(name, org),
            sub_harnesses={},
            gate_decisions={},
        )
        if state and state != "QUEUED":
            paused_at = ["gate-a"] if state == "PAUSED" else []
            await repo.transition(
                row.id,
                org,
                new_state=state,
                allowed_from=frozenset({"QUEUED"}),
                paused_at=paused_at,
            )
    return row.id


async def test_lists_the_orgs_runs_newest_first_with_team_name(repo: TeamRunRepository) -> None:
    svc = TeamRunService(team_runs=repo)
    ids = [await _seed(repo, ORG_A, USER_A, f"studio-{i}") for i in range(3)]

    rows, total = await svc.list_for_org(_principal(ORG_A, USER_A))
    assert total == 3
    assert {r["id"] for r in rows} == set(ids)  # every seeded run is listed
    # newest-first: created_at is non-increasing down the page (the #633 ordering contract)
    stamps = [r["created_at"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)
    # team_name is dug out of manifest.metadata.name (the manifest itself is never in the row)
    assert all(r["team_name"].startswith("studio-") for r in rows)
    assert all("manifest" not in r and "results" not in r for r in rows)


async def test_state_filter_returns_only_the_matching_runs(repo: TeamRunRepository) -> None:
    svc = TeamRunService(team_runs=repo)
    paused = await _seed(repo, ORG_A, USER_A, "paused-one", state="PAUSED")
    await _seed(repo, ORG_A, USER_A, "queued-one")  # stays QUEUED
    await _seed(repo, ORG_A, USER_A, "done-one", state="SUCCEEDED")

    rows, total = await svc.list_for_org(_principal(ORG_A, USER_A), states=["PAUSED"])
    assert total == 1 and [r["id"] for r in rows] == [paused]
    assert rows[0]["state"] == "PAUSED" and rows[0]["paused_at"] == ["gate-a"]


async def test_pagination_bounds_the_page_and_total_is_the_full_count(
    repo: TeamRunRepository,
) -> None:
    svc = TeamRunService(team_runs=repo)
    for i in range(5):
        await _seed(repo, ORG_A, USER_A, f"run-{i}")

    page1, total1 = await svc.list_for_org(_principal(ORG_A, USER_A), limit=2, offset=0)
    page2, total2 = await svc.list_for_org(_principal(ORG_A, USER_A), limit=2, offset=2)
    assert total1 == total2 == 5  # total is the FULL matching count, not the page length
    assert len(page1) == 2 and len(page2) == 2
    # the two pages are disjoint + continue the same newest-first ordering (stable paging)
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})
    assert page1[-1]["created_at"] >= page2[0]["created_at"]


@pytest.mark.organization_isolation
@pytest.mark.security
@pytest.mark.isolation
async def test_a_second_org_sees_zero(repo: TeamRunRepository) -> None:
    svc = TeamRunService(team_runs=repo)
    await _seed(repo, ORG_A, USER_A, "org-a-run")
    await _seed(repo, ORG_A, USER_A, "org-a-run-2", state="PAUSED")

    # org B lists its OWN team runs — org A's are RLS-filtered to absent (not a 403; invisible)
    rows, total = await svc.list_for_org(_principal(ORG_B, USER_B))
    assert rows == [] and total == 0
    # and the PAUSED filter is likewise empty for org B
    paused_rows, paused_total = await svc.list_for_org(_principal(ORG_B, USER_B), states=["PAUSED"])
    assert paused_rows == [] and paused_total == 0
    # org A still sees its own two
    a_rows, a_total = await svc.list_for_org(_principal(ORG_A, USER_A))
    assert a_total == 2
