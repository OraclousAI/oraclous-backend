"""#1109 — the suggested-form route: what the founder's browser receives when the DRAFTING run
itself is refused for a credential the provider rejected.

Mirrors ``test_intake_routes.py``'s
``test_a_provider_refused_reader_key_names_its_code_and_leaks_nothing`` (#1108) — same shape, same
reason: the gateway drains an upstream error body and only an allow-listed ``error_code`` crosses
it, so what matters is the route's mapping from ``AppFormDraftError`` to the HTTP body, not just
the service's own exception in isolation.

Runs the REAL ``AppFormDraftService`` (not a raising fake) against a fake ``TeamRunService`` whose
FAILED row carries ``member_error_codes`` — the same real-service-through-the-route pattern #1108
used, so a route that quietly drops the mapping (or a service that still raises the generic
``draft_failed`` 422) is caught here, not just in the service's own unit tests.

Seams imported FUNCTION-LOCALLY (``.claude/rules/tests-seam-imports.md``) — RED until the impl
lands.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from oraclous_execution_engine_service.app.factory import create_app
from oraclous_execution_engine_service.core.dependencies import get_principal
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()


def _client(service: Any) -> AsyncClient:
    from oraclous_execution_engine_service.core.dependencies import get_app_form_draft_service

    app = create_app()  # construction only — no lifespan, no DB
    app.dependency_overrides[get_app_form_draft_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


class _FailedDrafterRun:
    def __init__(self, member_error_codes: dict[str, str]) -> None:
        self.id = uuid.uuid4()
        self.state = "FAILED"
        self.results = None
        self.manifest = {"metadata": {"name": "app-form-drafter"}}
        self.member_error_codes = member_error_codes
        self.error_message = "sentinel default — overwritten by the test below"


class _FakeTeamRuns:
    """The ``TeamRunService`` seam the drafter polls — only ``.get`` is exercised on the collect
    path this test drives."""

    def __init__(self, row: _FailedDrafterRun) -> None:
        self._row = row

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _FailedDrafterRun:
        return self._row


class _FakeRunRepository:
    """The service constructor requires this seam, but the collect path (``form_draft_run_id``)
    never reads the source run through it — a call here is the test's own bug, not the route's."""

    async def get(self, run_id: uuid.UUID, organisation_id: uuid.UUID) -> Any:
        raise AssertionError("the collect path must never read the source run")


async def test_a_provider_refused_drafter_key_names_its_code_and_leaks_nothing() -> None:
    from oraclous_execution_engine_service.services.app_form_draft_service import (
        AppFormDraftService,
    )

    sentinel_key = "sk-or-v1-SENTINEL-DO-NOT-LEAK-4e91"
    sentinel_credential_id = "cred-SENTINEL-22cd"
    row = _FailedDrafterRun({"drafter": "llm_credential_rejected"})
    row.error_message = f"drafter rejected credential {sentinel_credential_id} ({sentinel_key})"

    service = AppFormDraftService(
        team_runs=_FakeTeamRuns(row),  # type: ignore[arg-type] — duck-typed seam
        team_run_repository=_FakeRunRepository(),  # type: ignore[arg-type]
        draft_poll_seconds=0.2,
        draft_poll_interval_seconds=0.01,
    )
    async with _client(service) as c:
        resp = await c.post(
            f"/v1/engine/team-runs/{uuid.uuid4()}/suggested-form",
            json={"form_draft_run_id": str(row.id)},
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == {"error_code": "MODEL_CREDENTIAL_REJECTED"}
    assert sentinel_key not in resp.text
    assert sentinel_credential_id not in resp.text
    assert row.error_message not in resp.text
