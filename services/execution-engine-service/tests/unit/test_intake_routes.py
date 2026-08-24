"""#866 — the read-back route: the shape the founder's browser actually receives.

``POST /v1/engine/intake/readback`` is the only user-facing door onto the read-back. What matters
at this layer is what crosses it:

- a 200 carrying the restatement as an ORDERED ARRAY of spans, never one blob of prose — the
  screen's whole job is to show the inferred spans *as* inferred so they can be corrected, and it
  cannot do that if it cannot tell them apart;
- a 202 carrying a run id when the model is slower than the poll budget, so slow does not mean
  failed;
- the two refusals named by their taxonomy code in the body, because the gateway drains an
  upstream error body and only an allow-listed code survives that boundary (#866 D2).

The service is faked through ``dependency_overrides``; the real model path belongs to the deployed
end-to-end run.

The route seams are imported FUNCTION-LOCALLY where needed
(``.claude/rules/tests-seam-imports.md``).
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

_GOOD_IDEA = (
    "An ordering tool for independent bakeries that still take their weekend orders "
    "on paper and lose track of half of them."
)

_MODELS = [
    {
        "role": "primary",
        "binding": "openrouter/x",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": "c1"},
    }
]


def _client(service: Any) -> AsyncClient:
    from oraclous_execution_engine_service.core.dependencies import get_intake_readback_service

    app = create_app()  # construction only — no lifespan, no DB
    app.dependency_overrides[get_intake_readback_service] = lambda: service
    app.dependency_overrides[get_principal] = lambda: Principal(
        principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://engine.test")


def _readback_result(run_id: uuid.UUID) -> Any:
    from oraclous_execution_engine_service.domain.intake_readback import Question, Span
    from oraclous_execution_engine_service.services.intake_readback_service import Readback

    return Readback(
        restatement=[
            Span(text="an ordering tool for independent bakeries ", source="read"),
            Span(text="whose owners lose paper orders", source="inferred"),
        ],
        questions=[
            Question(id="q1", text="How many orders a week?", kind="text", options=[]),
            Question(
                id="q2",
                text="Who pays?",
                kind="choice",
                options=["the bakery", "the customer"],
            ),
        ],
        readback_run_id=run_id,
    )


class _Ok:
    def __init__(self, run_id: uuid.UUID) -> None:
        self._run_id = run_id

    async def readback(self, principal: Principal, **kw: Any) -> Any:
        return _readback_result(self._run_id)


class _Raises:
    def __init__(self, status_code: int, error_code: str | None) -> None:
        self._status_code = status_code
        self._error_code = error_code

    async def readback(self, principal: Principal, **kw: Any) -> Any:
        from oraclous_execution_engine_service.services.intake_readback_service import (
            IntakeReadbackError,
        )

        raise IntakeReadbackError("refused", self._status_code, error_code=self._error_code)


# ── the answer ───────────────────────────────────────────────────────────────


async def test_a_read_idea_returns_spans_and_questions() -> None:
    run_id = uuid.uuid4()
    async with _client(_Ok(run_id)) as c:
        resp = await c.post(
            "/v1/engine/intake/readback", json={"idea": _GOOD_IDEA, "models": _MODELS}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restatement"] == [
        {"text": "an ordering tool for independent bakeries ", "source": "read"},
        {"text": "whose owners lose paper orders", "source": "inferred"},
    ]
    assert [q["id"] for q in body["questions"]] == ["q1", "q2"]
    assert body["questions"][1]["kind"] == "choice"
    assert body["questions"][1]["options"] == ["the bakery", "the customer"]
    assert body["questions"][0]["options"] == []


async def test_the_restatement_is_an_array_not_a_string() -> None:
    # Pinned separately because collapsing it to prose is the one change that would quietly
    # break the screen while every other assertion still passed.
    async with _client(_Ok(uuid.uuid4())) as c:
        resp = await c.post(
            "/v1/engine/intake/readback", json={"idea": _GOOD_IDEA, "models": _MODELS}
        )
    assert isinstance(resp.json()["restatement"], list)


async def test_the_idea_and_the_models_reach_the_service_unchanged() -> None:
    seen: dict[str, Any] = {}

    class _Spy:
        async def readback(self, principal: Principal, **kw: Any) -> Any:
            seen.update(kw)
            return _readback_result(uuid.uuid4())

    async with _client(_Spy()) as c:
        await c.post("/v1/engine/intake/readback", json={"idea": _GOOD_IDEA, "models": _MODELS})
    assert seen["idea"] == _GOOD_IDEA
    assert seen["models"][0]["config"]["credential_id"] == "c1"


# ── the slow model ───────────────────────────────────────────────────────────


async def test_a_pending_run_returns_202_with_the_run_id() -> None:
    run_id = uuid.uuid4()

    class _Pending:
        async def readback(self, principal: Principal, **kw: Any) -> Any:
            from oraclous_execution_engine_service.services.intake_readback_service import (
                PendingReadback,
            )

            return PendingReadback(run_id)

    async with _client(_Pending()) as c:
        resp = await c.post(
            "/v1/engine/intake/readback", json={"idea": _GOOD_IDEA, "models": _MODELS}
        )
    assert resp.status_code == 202
    assert resp.json() == {"readback_run_id": str(run_id), "status": "running"}


async def test_the_run_id_can_be_handed_back_to_collect() -> None:
    run_id = uuid.uuid4()
    seen: dict[str, Any] = {}

    class _Spy:
        async def readback(self, principal: Principal, **kw: Any) -> Any:
            seen.update(kw)
            return _readback_result(run_id)

    async with _client(_Spy()) as c:
        resp = await c.post("/v1/engine/intake/readback", json={"readback_run_id": str(run_id)})
    assert resp.status_code == 200
    assert seen["readback_run_id"] == run_id


async def test_neither_an_idea_nor_a_run_id_is_a_422() -> None:
    async with _client(object()) as c:
        resp = await c.post("/v1/engine/intake/readback", json={})
    assert resp.status_code == 422


# ── the refusals ─────────────────────────────────────────────────────────────


async def test_a_missing_model_is_a_409_naming_the_code() -> None:
    async with _client(_Raises(409, "MODEL_NOT_CONNECTED")) as c:
        resp = await c.post("/v1/engine/intake/readback", json={"idea": _GOOD_IDEA})
    assert resp.status_code == 409
    # the code has to be findable in the body — it is the only thing the gateway lets through
    assert "MODEL_NOT_CONNECTED" in resp.text


async def test_an_idea_under_the_floor_is_a_422_naming_the_code() -> None:
    async with _client(_Raises(422, "IDEA_TOO_VAGUE")) as c:
        resp = await c.post(
            "/v1/engine/intake/readback", json={"idea": "a bakery app", "models": _MODELS}
        )
    assert resp.status_code == 422
    assert "IDEA_TOO_VAGUE" in resp.text


async def test_a_refusal_with_no_code_still_maps_to_its_status() -> None:
    async with _client(_Raises(422, None)) as c:
        resp = await c.post(
            "/v1/engine/intake/readback", json={"idea": _GOOD_IDEA, "models": _MODELS}
        )
    assert resp.status_code == 422
