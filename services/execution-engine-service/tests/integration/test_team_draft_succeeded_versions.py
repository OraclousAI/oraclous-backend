"""Per-team succeeded-versions read + the ``has_succeeded_run`` list filter, against REAL
Postgres + the RLS backstop (#1163; docker-required).

Proven HERE, on the REAL ``TeamRunRepository``/``TeamDraftRepository`` wired exactly as the
request path wires them (the org-bound ``oraclous_app`` engine, the GUC guard installed):

- ``TeamDraftService.succeeded_versions`` returns, per team-draft version, the LATEST SUCCEEDED
  run only (highest ``updated_at``, i.e. its settle time — R13), newest version first, paginated,
  with a plain empty page for a draft that has never succeeded and a 404 for a foreign/unknown
  draft (R12, R16);
- ``TeamDraftService.list_for_org(..., has_succeeded_run=...)`` filters the org's own draft list
  by whether any of its runs ever settled SUCCEEDED (R14);
- neither read crosses an organisation: a same-id run filed under another org (no FK ties the
  columns together, R1) is invisible to the owning org's read, and a deleted draft still leaves
  its runs' ``team_draft_id`` in place (R1, "no backfill / a deleted team keeps its id on runs").

RED (#1163, ADR-010): none of
``TeamRunRepository.create(team_draft_id=..., team_draft_version=...)``,
``TeamDraftService.succeeded_versions`` or ``TeamDraftService.list_for_org(has_succeeded_run=...)``
exist yet. Every name here is called on an object that already exists (``.claude/rules/tests-seam-
imports.md`` applies to a MISSING MODULE, not this), so each test hard-fails at runtime with a
``TypeError``/``AttributeError`` on the missing seam rather than a collection error.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.repositories.team_draft_repository import (
    TeamDraftRepository,
)
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.team_draft_service import TeamDraftService
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
)
from oraclous_governance import Principal, PrincipalType

pytestmark = [pytest.mark.integration, pytest.mark.isolation]

ORG_A = uuid.UUID("a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1")
ORG_B = uuid.UUID("b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2")
USER_A = uuid.UUID("1a1a1a1a-1a1a-1a1a-1a1a-1a1a1a1a1a1a")
USER_B = uuid.UUID("2b2b2b2b-2b2b-2b2b-2b2b-2b2b2b2b2b2b")


def _principal(org: uuid.UUID, user: uuid.UUID) -> Principal:
    return Principal(principal_id=user, principal_type=PrincipalType.USER, organisation_id=org)


class _NoopProvenance:
    """#826: ``provenance`` is a non-optional ``TeamRunService`` kwarg; unrelated to the reads
    under test here, so a no-op stand-in is enough."""

    async def emit(self, record: Any) -> None:
        return None


def _run_manifest(name: str) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {"id": str(uuid.uuid4()), "name": name, "kind": "team"},
        "members": [{"role": "a", "kind": "agent"}],
    }


def _draft_manifest(org: uuid.UUID, name: str) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": name,
            "owner_organization_id": str(org),
            "kind": "team",
        },
        "members": [],
    }


@pytest.fixture
async def run_repo(engine_dsns) -> AsyncIterator[TeamRunRepository]:  # noqa: ANN001
    _owner, app_async_dsn = engine_dsns
    r = TeamRunRepository(app_async_dsn)
    try:
        yield r
    finally:
        await r.close()


@pytest.fixture
async def draft_repo(engine_dsns) -> AsyncIterator[TeamDraftRepository]:  # noqa: ANN001
    _owner, app_async_dsn = engine_dsns
    r = TeamDraftRepository(app_async_dsn)
    try:
        yield r
    finally:
        await r.close()


@pytest.fixture
def team_draft_service(
    draft_repo: TeamDraftRepository, run_repo: TeamRunRepository
) -> TeamDraftService:
    run_svc = TeamRunService(team_runs=run_repo, provenance=_NoopProvenance())  # type: ignore[arg-type]
    return TeamDraftService(drafts=draft_repo, team_runs=run_svc)


async def _draft(
    draft_repo: TeamDraftRepository, org: uuid.UUID, user: uuid.UUID, name: str
) -> uuid.UUID:
    with org_scope(org):
        row = await draft_repo.create(
            organisation_id=org,
            user_id=user,
            name=name,
            manifest=_draft_manifest(org, name),
            sub_harnesses={},
        )
    return row.id


async def _seed(
    run_repo: TeamRunRepository,
    org: uuid.UUID,
    user: uuid.UUID,
    draft_id: uuid.UUID | None,
    version: int | None,
    state: str,
) -> uuid.UUID:
    """Create a QUEUED run tagged with ``(draft_id, version)`` and CAS it straight to ``state``
    (mirrors ``test_team_run_list_integration._seed``). RED: ``TeamRunRepository.create`` does not
    accept ``team_draft_id``/``team_draft_version`` yet, so this raises ``TypeError`` until I1
    lands — the seam call every test below depends on."""
    with org_scope(org):
        row = await run_repo.create(
            organisation_id=org,
            user_id=user,
            manifest=_run_manifest(f"run-v{version}"),
            sub_harnesses={},
            gate_decisions={},
            team_draft_id=draft_id,
            team_draft_version=version,
        )
        if state != "QUEUED":
            await run_repo.transition(
                row.id, org, new_state=state, allowed_from=frozenset({"QUEUED"})
            )
    return row.id


async def test_the_latest_succeeded_run_per_version_orders_versions_newest_first(
    team_draft_service: TeamDraftService,
    draft_repo: TeamDraftRepository,
    run_repo: TeamRunRepository,
) -> None:
    draft_id = await _draft(draft_repo, ORG_A, USER_A, "team-versions")
    other_draft_id = await _draft(draft_repo, ORG_A, USER_A, "team-versions-other")

    await _seed(run_repo, ORG_A, USER_A, draft_id, 1, "SUCCEEDED")  # r1: superseded below
    r2 = await _seed(run_repo, ORG_A, USER_A, draft_id, 1, "SUCCEEDED")  # seeded after r1: later
    await _seed(run_repo, ORG_A, USER_A, draft_id, 2, "FAILED")  # v2 never succeeded
    r3 = await _seed(run_repo, ORG_A, USER_A, draft_id, 3, "SUCCEEDED")
    await _seed(run_repo, ORG_A, USER_A, draft_id, 3, "COST_BUDGET")  # not a success (R10)
    await _seed(run_repo, ORG_A, USER_A, draft_id, 4, "COST_BUDGET")  # v4: no success at all
    await _seed(run_repo, ORG_A, USER_A, None, None, "SUCCEEDED")  # no team at all
    await _seed(run_repo, ORG_A, USER_A, other_draft_id, 1, "SUCCEEDED")  # a different draft

    versions, total = await team_draft_service.succeeded_versions(
        draft_id, _principal(ORG_A, USER_A)
    )

    assert total == 2
    assert [v["version"] for v in versions] == [3, 1]

    with org_scope(ORG_A):
        r2_row = await run_repo.get(r2, ORG_A)
    by_version = {v["version"]: v for v in versions}
    assert by_version[1]["team_run_id"] == r2
    assert by_version[1]["finished_at"] == r2_row.updated_at
    assert by_version[3]["team_run_id"] == r3


async def test_pagination_bounds_the_versions_page(
    team_draft_service: TeamDraftService,
    draft_repo: TeamDraftRepository,
    run_repo: TeamRunRepository,
) -> None:
    draft_id = await _draft(draft_repo, ORG_A, USER_A, "team-paginated")
    await _seed(run_repo, ORG_A, USER_A, draft_id, 1, "SUCCEEDED")
    await _seed(run_repo, ORG_A, USER_A, draft_id, 2, "SUCCEEDED")

    versions, total = await team_draft_service.succeeded_versions(
        draft_id, _principal(ORG_A, USER_A), limit=1, offset=1
    )

    assert total == 2
    assert [v["version"] for v in versions] == [1]


async def test_a_draft_with_no_succeeded_runs_gives_an_empty_page(
    team_draft_service: TeamDraftService, draft_repo: TeamDraftRepository
) -> None:
    draft_id = await _draft(draft_repo, ORG_A, USER_A, "team-no-runs-at-all")

    versions, total = await team_draft_service.succeeded_versions(
        draft_id, _principal(ORG_A, USER_A)
    )

    assert versions == []
    assert total == 0


async def test_a_foreign_or_unknown_draft_id_is_a_404(
    team_draft_service: TeamDraftService, draft_repo: TeamDraftRepository
) -> None:
    draft_id = await _draft(draft_repo, ORG_A, USER_A, "team-org-a-only")

    with pytest.raises(TeamRunError) as foreign:
        await team_draft_service.succeeded_versions(draft_id, _principal(ORG_B, USER_B))
    assert foreign.value.status_code == 404

    with pytest.raises(TeamRunError) as unknown:
        await team_draft_service.succeeded_versions(uuid.uuid4(), _principal(ORG_A, USER_A))
    assert unknown.value.status_code == 404


async def test_a_same_id_run_filed_under_another_org_is_invisible(
    team_draft_service: TeamDraftService,
    draft_repo: TeamDraftRepository,
    run_repo: TeamRunRepository,
) -> None:
    draft_id = await _draft(draft_repo, ORG_A, USER_A, "team-shared-draft-id")
    r_a = await _seed(run_repo, ORG_A, USER_A, draft_id, 1, "SUCCEEDED")
    # No FK ties team_draft_id to a draft (R1) — a run filed under ORG_B that happens to carry
    # ORG_A's draft id must still stay out of ORG_A's read; only organisation_id decides.
    await _seed(run_repo, ORG_B, USER_B, draft_id, 1, "SUCCEEDED")

    versions, total = await team_draft_service.succeeded_versions(
        draft_id, _principal(ORG_A, USER_A)
    )

    assert total == 1
    assert versions[0]["team_run_id"] == r_a


async def test_has_succeeded_run_filters_the_orgs_draft_list(
    team_draft_service: TeamDraftService,
    draft_repo: TeamDraftRepository,
    run_repo: TeamRunRepository,
) -> None:
    with_success = await _draft(draft_repo, ORG_A, USER_A, "team-with-a-success")
    failed_only = await _draft(draft_repo, ORG_A, USER_A, "team-failed-only")
    no_runs = await _draft(draft_repo, ORG_A, USER_A, "team-no-runs")

    await _seed(run_repo, ORG_A, USER_A, with_success, 1, "SUCCEEDED")
    await _seed(run_repo, ORG_A, USER_A, failed_only, 1, "FAILED")

    only_success, total_success = await team_draft_service.list_for_org(
        _principal(ORG_A, USER_A), has_succeeded_run=True
    )
    assert {r["id"] for r in only_success} == {with_success}
    assert total_success == 1

    without_success, total_without = await team_draft_service.list_for_org(
        _principal(ORG_A, USER_A), has_succeeded_run=False
    )
    assert {r["id"] for r in without_success} == {failed_only, no_runs}
    assert total_without == 2

    every_draft, total_all = await team_draft_service.list_for_org(
        _principal(ORG_A, USER_A), has_succeeded_run=None
    )
    assert {r["id"] for r in every_draft} == {with_success, failed_only, no_runs}
    assert total_all == 3

    org_b_page, org_b_total = await team_draft_service.list_for_org(
        _principal(ORG_B, USER_B), has_succeeded_run=True
    )
    assert org_b_page == []
    assert org_b_total == 0


async def test_deleting_the_draft_404s_the_read_but_keeps_the_runs_team_draft_id(
    team_draft_service: TeamDraftService,
    draft_repo: TeamDraftRepository,
    run_repo: TeamRunRepository,
) -> None:
    draft_id = await _draft(draft_repo, ORG_A, USER_A, "team-to-be-deleted")
    run_id = await _seed(run_repo, ORG_A, USER_A, draft_id, 1, "SUCCEEDED")

    with org_scope(ORG_A):
        deleted = await draft_repo.delete(draft_id, ORG_A)
    assert deleted is True

    with pytest.raises(TeamRunError) as excinfo:
        await team_draft_service.succeeded_versions(draft_id, _principal(ORG_A, USER_A))
    assert excinfo.value.status_code == 404

    with org_scope(ORG_A):
        row = await run_repo.get(run_id, ORG_A)
    assert row.team_draft_id == draft_id
