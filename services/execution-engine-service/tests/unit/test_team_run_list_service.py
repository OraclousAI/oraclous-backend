"""TeamRunService.list_for_org — org scoping + server-side bounds clamp (#633; unit, fake repo).

The service resolves the org from the principal ONLY (a principal with no org is a 403), clamps the
page bounds server-side (``limit`` into [1, 200] default 50, ``offset`` >= 0) so no read is
unbounded, and passes the (state filter, clamped bounds) through to the repo, returning its
``(rows, total)`` unchanged. The RLS org-scoping is proven against real Postgres in the integration
test; here the repo is faked to pin the clamp + forwarding logic without a DB.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run_service import TeamRunError, TeamRunService
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _FakeRepo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_for_org(
        self,
        organisation_id: uuid.UUID,
        *,
        states: Any = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(
            {"org": organisation_id, "states": states, "limit": limit, "offset": offset}
        )
        return [{"id": uuid.uuid4()}], 42


def _service(repo: _FakeRepo) -> TeamRunService:
    return TeamRunService(team_runs=repo)  # type: ignore[arg-type]


async def test_returns_repo_rows_and_total_unchanged() -> None:
    repo = _FakeRepo()
    rows, total = await _service(repo).list_for_org(_principal())
    assert total == 42 and len(rows) == 1  # the FULL matching total flows straight through


async def test_org_comes_from_the_principal() -> None:
    repo = _FakeRepo()
    await _service(repo).list_for_org(_principal())
    assert repo.calls[0]["org"] == _ORG  # scoped to the authenticated principal's org only


async def test_a_principal_without_an_org_is_403() -> None:
    with pytest.raises(TeamRunError) as exc:
        await _service(_FakeRepo()).list_for_org(_principal(org=None))
    assert exc.value.status_code == 403  # fail-closed tenancy (ADR-006)


async def test_states_are_forwarded() -> None:
    repo = _FakeRepo()
    await _service(repo).list_for_org(_principal(), states=["PAUSED", "RUNNING"])
    assert repo.calls[0]["states"] == ["PAUSED", "RUNNING"]


@pytest.mark.parametrize(
    ("given", "clamped"),
    [(50, 50), (200, 200), (201, 200), (1000, 200), (1, 1), (0, 1), (-5, 1)],
)
async def test_limit_is_clamped_to_1_200(given: int, clamped: int) -> None:
    repo = _FakeRepo()
    await _service(repo).list_for_org(_principal(), limit=given)
    assert repo.calls[0]["limit"] == clamped  # bounds clamp — no single read is unbounded


@pytest.mark.parametrize(("given", "clamped"), [(0, 0), (5, 5), (-1, 0), (-100, 0)])
async def test_offset_is_clamped_to_non_negative(given: int, clamped: int) -> None:
    repo = _FakeRepo()
    await _service(repo).list_for_org(_principal(), offset=given)
    assert repo.calls[0]["offset"] == clamped


async def test_the_default_page_is_limit_50_offset_0() -> None:
    repo = _FakeRepo()
    await _service(repo).list_for_org(_principal())
    assert repo.calls[0]["limit"] == 50 and repo.calls[0]["offset"] == 0
