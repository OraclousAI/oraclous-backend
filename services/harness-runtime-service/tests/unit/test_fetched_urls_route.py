"""#975 (§CITE cite-by-reference), plan §4 — the `/execute` route forwards the two new request
fields straight to the service (routes carry no logic: CLAUDE.md's layered-service invariant).

There is no existing route-level unit test for `execute_harness` to mirror byte-for-byte (the
service's own fake-registry harness in `test_memory_hook.py`/`test_resume_service.py` is the closest
precedent for this service's fake-collaborator style); this file calls the route coroutine directly
with a duck-typed request body and a capturing fake service, the same way those files fake the
service's own collaborators — no ASGI app, no FastAPI dependency wiring, since none of that logic
lives in the route.

RED today: the route reads neither `body.prior_fetched_urls` nor `body.person_supplied_text`, so
they never reach `service.execute(...)` — a missing-key assertion failure on the service's captured
kwargs, not a mere constructor `TypeError`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_A = "https://example.com/a"


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
        required_sites=[],
        prior_fetched_urls=[_A],
        person_supplied_text="the task",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _principal() -> Principal:
    return Principal(
        principal_id=uuid.uuid4(), principal_type=PrincipalType.USER, organisation_id=uuid.uuid4()
    )


class _CapturingService:
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


async def test_execute_route_forwards_prior_fetched_urls_and_person_supplied_text() -> None:
    from oraclous_harness_runtime_service.routes.harness_routes import execute_harness

    service = _CapturingService()
    await execute_harness(_body(), _principal(), service)  # type: ignore[arg-type]

    assert service.kwargs is not None
    # today: absent — the route never reads these two attributes off the request body.
    assert service.kwargs["prior_fetched_urls"] == [_A]
    assert service.kwargs["person_supplied_text"] == "the task"
