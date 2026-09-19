"""``TeamRunService.succeeded_versions_for_draft`` — org scoping + bounds clamp (#1163; unit, fake
repo, no DB), mirroring ``test_team_run_list_service.py``'s pattern for ``list_for_org``.

The service resolves the org from the principal ONLY (a principal with no org is a 403), clamps
``limit``/``offset`` server-side exactly as ``list_for_org`` does, and passes the (org, draft id,
clamped bounds) through to the repo, returning its ``(rows, total)`` unchanged. The RLS org-scoping
and the real SQL (DISTINCT-per-version, latest-first) are proven against real Postgres in the
integration test; here the repo is faked to pin the clamp + forwarding logic without a DB.

RED (#1163, ADR-010): ``TeamRunService`` has no ``succeeded_versions_for_draft`` method yet. It is
called on an object (``TeamRunService``) that already exists, so this is a missing METHOD, not a
missing module — no seam-import concern; the test hard-fails at runtime with an ``AttributeError``.
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
_DRAFT = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _FakeRepo:
    def __init__(self, rows: list[dict[str, Any]] | None = None, total: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._rows = rows if rows is not None else []
        self._total = total

    async def succeeded_versions_for_draft(
        self,
        organisation_id: uuid.UUID,
        team_draft_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(
            {"org": organisation_id, "draft": team_draft_id, "limit": limit, "offset": offset}
        )
        return self._rows, self._total


class _NoopProvenance:
    """#826: ``provenance`` is a non-optional ``TeamRunService`` kwarg; unrelated here (clamp /
    forwarding behaviour), so a no-op stand-in is enough."""

    async def emit(self, record: Any) -> None:
        return None


def _service(repo: _FakeRepo) -> TeamRunService:
    return TeamRunService(team_runs=repo, provenance=_NoopProvenance())  # type: ignore[arg-type]


async def test_org_comes_from_the_principal() -> None:
    repo = _FakeRepo()
    await _service(repo).succeeded_versions_for_draft(_principal(), _DRAFT)
    assert repo.calls[0]["org"] == _ORG
    assert repo.calls[0]["draft"] == _DRAFT


async def test_limit_and_offset_are_clamped_to_bounds() -> None:
    repo = _FakeRepo()
    svc = _service(repo)

    await svc.succeeded_versions_for_draft(_principal(), _DRAFT, limit=0)
    assert repo.calls[0]["limit"] == 1  # floor

    await svc.succeeded_versions_for_draft(_principal(), _DRAFT, limit=10_000)
    assert repo.calls[1]["limit"] == 200  # ceiling

    await svc.succeeded_versions_for_draft(_principal(), _DRAFT, offset=-5)
    assert repo.calls[2]["offset"] == 0  # never negative


async def test_a_principal_without_an_org_is_403() -> None:
    with pytest.raises(TeamRunError) as exc:
        await _service(_FakeRepo()).succeeded_versions_for_draft(_principal(org=None), _DRAFT)
    assert exc.value.status_code == 403  # fail-closed tenancy (ADR-006)


async def test_rows_and_total_pass_through_unchanged() -> None:
    run_id = uuid.uuid4()
    row = {"version": 3, "team_run_id": run_id, "finished_at": None}
    repo = _FakeRepo(rows=[row], total=7)
    rows, total = await _service(repo).succeeded_versions_for_draft(_principal(), _DRAFT)
    assert rows == [row]
    assert total == 7
