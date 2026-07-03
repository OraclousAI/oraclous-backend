"""Team-draft store against a REAL Postgres + the RLS backstop (ADR-030) — docker-required.

The #635 draft store end-to-end on the substrate it ships on: the REAL ``TeamDraftService`` +
``TeamDraftRepository`` on the NOSUPERUSER ``oraclous_app`` org-bound engine (the GUC guard
installed by default), wired exactly as ``get_team_draft_service`` wires it in deployment. Proven
HERE: a draft PERSISTS with its validator verdict, READS BACK under the request-bound org,
version-bumps on replace/refine, and is ISOLATED across orgs by the RLS policy on
``engine_team_drafts`` (a second org sees ZERO drafts; a cross-org read/replace/delete is a 404).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_execution_engine_service.repositories.team_draft_repository import (
    TeamDraftRepository,
)
from oraclous_execution_engine_service.services.team_draft_service import TeamDraftService
from oraclous_execution_engine_service.services.team_run_service import TeamRunError
from oraclous_governance import Principal, PrincipalType

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _principal(org: uuid.UUID, user: uuid.UUID) -> Principal:
    return Principal(principal_id=user, principal_type=PrincipalType.USER, organisation_id=org)


def _team(org: uuid.UUID, roles: list[str]) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "draft-team",
            "owner_organization_id": str(org),
            "kind": "team",
        },
        "members": [
            {
                "role": r,
                "kind": "agent",
                "manifest_ref": f"org:x/{r}@1",
                "subgoal": f"do {r}",
                "depends_on": [],
            }
            for r in roles
        ],
        "runtime": {"entrypoint": roles[0]},
    }


@pytest.fixture
async def team_draft_service(engine_dsns) -> AsyncIterator[TeamDraftService]:  # noqa: ANN001
    """The REAL request-path TeamDraftService on the org-bound ``oraclous_app`` engine — the
    service binds the org per request via ``org_scope`` (the repo's GUC guard then sets the RLS
    GUC). The store paths never touch team runs, so the run seam is a bare stub here."""
    _owner_async_dsn, app_async_dsn = engine_dsns
    repo = TeamDraftRepository(app_async_dsn)
    try:
        yield TeamDraftService(drafts=repo, team_runs=object())  # type: ignore[arg-type]
    finally:
        await repo.close()


async def test_draft_persists_reads_back_and_version_bumps_on_org_bound_engine(
    team_draft_service: TeamDraftService,
) -> None:
    """create on the org-bound engine binds the org itself, so the RLS-backstopped INSERT is
    admitted (without org_scope it would raise 42501 against the empty GUC) — and the tenant
    reads its own draft back, with the shared validator's verdict on every round-trip."""
    row, verdict = await team_draft_service.create(
        _principal(ORG_A, USER_A),
        name="draft-a",
        manifest=_team(ORG_A, ["researcher", "writer"]),
        sub_harnesses={},
    )
    assert row.organisation_id == ORG_A and row.version == 1
    assert verdict.would_block is False

    fetched, fetched_verdict = await team_draft_service.get(row.id, _principal(ORG_A, USER_A))
    assert fetched.id == row.id and fetched.name == "draft-a"
    assert fetched_verdict.would_block is False

    replaced, _ = await team_draft_service.replace(
        row.id,
        _principal(ORG_A, USER_A),
        name="draft-a2",
        manifest=_team(ORG_A, ["researcher"]),
        sub_harnesses={},
    )
    assert replaced.version == 2 and replaced.name == "draft-a2"

    outcome = await team_draft_service.refine(
        row.id,
        _principal(ORG_A, USER_A),
        edit_op={"op": "add_member", "role": "fact-checker", "depends_on": ["researcher"]},
    )
    assert outcome.applied is True and outcome.row.version == 3
    assert "fact-checker" in outcome.row.sub_harnesses  # synthesized + persisted

    rows, total = await team_draft_service.list_for_org(_principal(ORG_A, USER_A))
    assert total == 1 and rows[0]["name"] == "draft-a2"
    assert rows[0]["member_count"] == 2  # researcher + fact-checker, dug at query time
    assert "manifest" not in rows[0]  # the list row is LEAN


async def test_a_second_org_sees_zero_drafts(team_draft_service: TeamDraftService) -> None:
    await team_draft_service.create(
        _principal(ORG_A, USER_A),
        name="a-draft",
        manifest=_team(ORG_A, ["researcher"]),
        sub_harnesses={},
    )
    rows, total = await team_draft_service.list_for_org(_principal(ORG_B, USER_B))
    assert rows == [] and total == 0  # RLS + app-layer scoping: org B sees ZERO


async def test_cross_org_read_replace_delete_are_404(
    team_draft_service: TeamDraftService,
) -> None:
    row, _ = await team_draft_service.create(
        _principal(ORG_A, USER_A),
        name="a-draft",
        manifest=_team(ORG_A, ["researcher"]),
        sub_harnesses={},
    )
    for attempt in ("get", "replace", "delete", "refine"):
        with pytest.raises(TeamRunError) as exc:
            if attempt == "get":
                await team_draft_service.get(row.id, _principal(ORG_B, USER_B))
            elif attempt == "replace":
                await team_draft_service.replace(
                    row.id,
                    _principal(ORG_B, USER_B),
                    name="stolen",
                    manifest=_team(ORG_B, ["x"]),
                    sub_harnesses={},
                )
            elif attempt == "delete":
                await team_draft_service.delete(row.id, _principal(ORG_B, USER_B))
            else:
                await team_draft_service.refine(
                    row.id,
                    _principal(ORG_B, USER_B),
                    edit_op={"op": "add_member", "role": "y"},
                )
        assert exc.value.status_code == 404  # invisible, never a 403

    # org A's draft is untouched by every cross-org attempt
    kept, _ = await team_draft_service.get(row.id, _principal(ORG_A, USER_A))
    assert kept.name == "a-draft" and kept.version == 1
