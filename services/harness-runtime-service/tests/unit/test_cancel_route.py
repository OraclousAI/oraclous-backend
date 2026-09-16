"""#1072 (T3) — route/schema layer for harness cancellation. Routes carry no logic (CLAUDE.md's
layered-service invariant): ``POST /v1/harnesses/{execution_id}/cancel`` (new) forwards straight
to ``HarnessExecutionService.cancel(execution_id=..., organisation_id=...)`` (T2 seam) and maps
its three outcomes to HTTP — a terminal row -> 200 with the execution body; ``CancelPending`` ->
202 (dynamic status via an injected ``Response``, the same shape
``community_routes.detect_communities`` already uses for a sync/async split); ``CancelError`` ->
404. No organisation bound on the caller -> 401, exactly like every other org-scoped route in this
file via ``_require_org``. ``ExecuteHarnessRequest`` (existing schema) gains ``execution_id``
(#1072 design) — accepted and forwarded to ``service.execute(...)``; the existing ``/execute``
route maps the lease repository's ``DuplicateExecutionId`` to 409.

There is no existing route-level unit test for ``cancel_harness`` to mirror (it does not exist
yet); this file follows ``test_fetched_urls_route.py``'s technique — call the route coroutine
directly with a duck-typed request body / capturing fake service, no ASGI app, no FastAPI
dependency wiring, since none of that logic lives in the route.

**Not-yet-built seams**, pinned by this file (each imported function-locally, never at module
level, per ``.claude/rules/tests-seam-imports.md`` — ``cancel_harness`` is a NEW NAME in an
EXISTING module, so importing it raises ``ImportError`` at import time, not merely
``AttributeError`` at the call site, and would abort collection for the whole run if done at
module level):

* ``oraclous_harness_runtime_service.routes.harness_routes.cancel_harness`` — the route itself.
  Today: ``ImportError`` (no such name in the module).
* ``oraclous_harness_runtime_service.services.harness_execution_service.CancelPending`` /
  ``CancelError`` — T2's markers, used here only to drive the route's fake service.
* ``oraclous_harness_runtime_service.repositories.execution_lease_repository.DuplicateExecutionId``
  — T4's seam (new module); the execute route's 409 mapping test raises it from a fake service.

``ExecuteHarnessRequest`` and ``HarnessStatus`` already exist (only a field / an enum member is
missing), so importing those classes themselves is safe at module level; only touching the missing
attribute at runtime fails (``AttributeError``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response, status
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.schema.harness_schemas import ExecuteHarnessRequest

pytestmark = pytest.mark.unit

_UNSET = object()


def _principal(*, organisation_id: uuid.UUID | None | object = _UNSET) -> Principal:
    return Principal(
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
        organisation_id=uuid.uuid4() if organisation_id is _UNSET else organisation_id,  # type: ignore[arg-type]
    )


def _body(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        manifest_yaml=None,
        manifest={"ohm_version": "1.0"},
        manifest_ref=None,
        input="go",
        capability_ceiling=None,
        parent_execution_id=None,
        trace_id=None,
        workspace_root=None,
        graph_id=None,
        team_id=None,
        producer=None,
        precedence_order=None,
        graph_authoritative=False,
        max_tokens=None,
        max_tool_calls=None,
        on_exhaustion=None,
        requires_valid_json=False,
        answer_from_tool=None,
        required_sites=[],
        declared_output_keys=[],
        prior_fetched_urls=None,
        person_supplied_text=None,
        execution_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _CapturingExecuteService:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def execute(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=kwargs["principal"].organisation_id,
            harness_id=uuid.uuid4(),
            harness_name="T",
            status="SUCCEEDED",
            output="ok",
            error_type=None,
            error_message=None,
            iterations=1,
            total_tokens=0,
            steps=[],
            created_at=None,
            content_hash=None,
        )


class _CapturingCancelService:
    """Fake ``HarnessExecutionService``-shaped collaborator for the cancel route: ``result`` is
    returned from ``cancel()``, or ``error`` (if set) is raised instead."""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.kwargs: dict[str, Any] | None = None

    async def cancel(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.result


# ---------------------------------------------------------------------------
# Schema: ExecuteHarnessRequest.execution_id
# ---------------------------------------------------------------------------


def test_execute_harness_request_accepts_execution_id() -> None:
    execution_id = uuid.uuid4()
    req = ExecuteHarnessRequest(
        manifest={"ohm_version": "1.0"}, input="go", execution_id=execution_id
    )

    # today: the field does not exist, so the constructor silently drops the unknown kwarg
    # (no ``model_config`` on this class -> pydantic v2 default ``extra="ignore"``) and this
    # attribute access raises AttributeError.
    assert req.execution_id == execution_id


# ---------------------------------------------------------------------------
# /execute: forwards execution_id, maps DuplicateExecutionId to 409
# ---------------------------------------------------------------------------


async def test_execute_route_forwards_execution_id_to_service() -> None:
    from oraclous_harness_runtime_service.routes.harness_routes import execute_harness

    execution_id = uuid.uuid4()
    service = _CapturingExecuteService()

    await execute_harness(  # type: ignore[arg-type]
        _body(execution_id=execution_id), _principal(), service
    )

    assert service.kwargs is not None
    # today: KeyError — the route never reads body.execution_id.
    assert service.kwargs["execution_id"] == execution_id


async def test_execute_route_maps_duplicate_execution_id_to_409() -> None:
    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        DuplicateExecutionId,
    )
    from oraclous_harness_runtime_service.routes.harness_routes import execute_harness

    class _DuplicateService:
        async def execute(self, **kwargs: Any) -> Any:
            raise DuplicateExecutionId(kwargs["execution_id"])

    with pytest.raises(HTTPException) as exc_info:
        await execute_harness(  # type: ignore[arg-type]
            _body(execution_id=uuid.uuid4()), _principal(), _DuplicateService()
        )

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT


# ---------------------------------------------------------------------------
# POST /v1/harnesses/{execution_id}/cancel
# ---------------------------------------------------------------------------


async def test_cancel_route_returns_200_with_cancelled_row_and_forwards_org() -> None:
    from oraclous_harness_runtime_service.models.enums import HarnessStatus
    from oraclous_harness_runtime_service.routes.harness_routes import cancel_harness

    execution_id = uuid.uuid4()
    principal = _principal()
    row = SimpleNamespace(
        id=execution_id,
        organisation_id=principal.organisation_id,
        harness_id=uuid.uuid4(),
        harness_name="T",
        content_hash=None,
        status=HarnessStatus.CANCELLED,
        output=None,
        error_type=None,
        error_message=None,
        iterations=2,
        total_tokens=100,
        steps=[],
        created_at=None,
    )
    service = _CapturingCancelService(result=row)
    response = Response()

    result = await cancel_harness(execution_id, principal, service, response)  # type: ignore[arg-type]

    assert response.status_code == status.HTTP_200_OK
    assert result.status == HarnessStatus.CANCELLED
    assert result.total_tokens == 100
    # the route resolves the org from the caller's principal and passes it to cancel(), never a
    # client-supplied value.
    assert service.kwargs == {
        "execution_id": execution_id,
        "organisation_id": principal.organisation_id,
    }


async def test_cancel_route_returns_202_cancel_requested_when_pending() -> None:
    from oraclous_harness_runtime_service.routes.harness_routes import cancel_harness
    from oraclous_harness_runtime_service.services.harness_execution_service import CancelPending

    execution_id = uuid.uuid4()
    principal = _principal()
    pending = CancelPending(execution_id=execution_id)
    service = _CapturingCancelService(result=pending)
    response = Response()

    result = await cancel_harness(execution_id, principal, service, response)  # type: ignore[arg-type]

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert result is pending
    assert result.execution_id == execution_id
    assert result.status == "CANCEL_REQUESTED"


async def test_cancel_route_raises_404_when_cancel_error() -> None:
    from oraclous_harness_runtime_service.routes.harness_routes import cancel_harness
    from oraclous_harness_runtime_service.services.harness_execution_service import CancelError

    execution_id = uuid.uuid4()
    service = _CapturingCancelService(error=CancelError("execution not found"))
    response = Response()

    with pytest.raises(HTTPException) as exc_info:
        await cancel_harness(execution_id, _principal(), service, response)  # type: ignore[arg-type]

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


async def test_cancel_route_raises_401_when_no_organisation_bound() -> None:
    from oraclous_harness_runtime_service.routes.harness_routes import cancel_harness

    execution_id = uuid.uuid4()
    principal = _principal(organisation_id=None)
    service = _CapturingCancelService()
    response = Response()

    with pytest.raises(HTTPException) as exc_info:
        await cancel_harness(execution_id, principal, service, response)  # type: ignore[arg-type]

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    # unauthenticated caller never reaches the service.
    assert service.kwargs is None
