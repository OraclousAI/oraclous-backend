"""core/evaluate — the flow-level judge HTTP layer (ADR-037 / #469). Real route + dev-auth + a fake
judge. Covers: the Verdict envelope, auth (401), the typed no-judge 422, the battery-deferred 422,
and the H2 org-stamping security invariant (the graded org is the principal's, never the body)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.core.dependencies import get_eval_judge_optional

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}


def _body(**over: object) -> dict:
    base = {
        "target_kind": "member_output",
        "target_ref": "run-1/member-a",
        "target_output": "Ada Lovelace wrote the first computer program.",
        "success_criteria": "the answer is factually correct and complete",
    }
    base.update(over)
    return base


class _FakeJudge:
    def __init__(self, score: float = 0.9) -> None:
        self._score = score

    async def complete_json(self, *, system: str, user: str) -> str:
        return f'{{"score": {self._score}, "reason": "ok"}}'

    async def complete_text(self, *, system: str, user: str) -> str:
        return "text"


@pytest.fixture
def client(app, async_client):
    app.dependency_overrides[get_eval_judge_optional] = lambda: _FakeJudge()
    yield async_client
    app.dependency_overrides.clear()


async def test_returns_a_structured_verdict(client) -> None:
    resp = await client.post("/internal/v1/evaluate", json=_body(), headers=_AUTH)
    assert resp.status_code == 200, resp.text
    v = resp.json()
    assert v["pass"] is True and v["score"] == 0.9
    assert v["dimension_scores"] == {"success_criteria": 0.9}
    assert v["recommended_action"] == "accept"
    assert v["evaluated"]["target_ref"] == "run-1/member-a"


async def test_below_threshold_does_not_pass(app, client) -> None:
    app.dependency_overrides[get_eval_judge_optional] = lambda: _FakeJudge(score=0.2)
    resp = await client.post("/internal/v1/evaluate", json=_body(), headers=_AUTH)
    assert resp.json()["pass"] is False and resp.json()["recommended_action"] == "revise"


async def test_requires_auth(client) -> None:
    assert (await client.post("/internal/v1/evaluate", json=_body())).status_code == 401


async def test_no_judge_configured_is_typed_422(app, async_client) -> None:
    # no get_eval_judge_optional override → app.state.eval_judge unset → the typed 422 (no fakes)
    resp = await async_client.post("/internal/v1/evaluate", json=_body(), headers=_AUTH)
    assert resp.status_code == 422


async def test_named_battery_criterion_is_deferred_422(client) -> None:
    resp = await client.post(
        "/internal/v1/evaluate",
        json=_body(success_criteria="battery:report-editor-10gate"),
        headers=_AUTH,
    )
    assert resp.status_code == 422


@pytest.mark.security
async def test_graded_org_is_server_stamped_not_from_body(client) -> None:
    """ADR-037 H2: the request carries no org; even a smuggled body ``organisation_id`` is ignored —
    the verdict's org is the authenticated principal's, so a caller cannot forge a foreign-org
    verdict."""
    evil = str(uuid.uuid4())
    resp = await client.post(
        "/internal/v1/evaluate", json=_body(organisation_id=evil), headers=_AUTH
    )
    assert resp.status_code == 200, resp.text
    org = resp.json()["evaluated"]["organisation_id"]
    assert org != evil  # the smuggled body org never wins
    assert org == get_settings().dev_org_id  # it is the principal's (dev) org, server-stamped


async def test_byom_judge_credential_grades_via_broker_without_singleton(
    app, async_client, monkeypatch
) -> None:
    """ADR-037 / BYOM-judge: a judge_credential_id makes KRS resolve the caller's own key per-org
    from the broker and grade with a PER-REQUEST judge — with NO operator singleton configured
    (app.state.eval_judge unset). This is the unit-level negative control: the BYOM path must NOT
    need KRS_OPENAI_API_KEY, and the per-request judge must be aclose()'d."""
    closed: list[bool] = []

    class _ByomJudge:
        async def complete_json(self, *, system: str, user: str) -> str:
            return '{"score": 0.95, "reason": "ok"}'

        async def complete_text(self, *, system: str, user: str) -> str:
            return "t"

        async def aclose(self) -> None:
            closed.append(True)

    async def _fake_resolve(settings, *, credential_id, judge_model, organisation_id):  # noqa: ANN001
        assert credential_id == "cred-byom"  # the request's credential reached the resolver
        return _ByomJudge()

    import oraclous_knowledge_retriever_service.routes.internal_routes as ir

    monkeypatch.setattr(ir, "resolve_byom_judge", _fake_resolve)
    # NO get_eval_judge_optional override → the singleton is None; only the BYOM path can succeed.
    resp = await async_client.post(
        "/internal/v1/evaluate", json=_body(judge_credential_id="cred-byom"), headers=_AUTH
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pass"] is True and resp.json()["score"] == 0.95
    assert closed == [True]  # the per-request judge was closed (no client leak)


async def test_byom_credential_unresolvable_is_fail_closed_422(
    app, async_client, monkeypatch
) -> None:
    """An unresolvable BYOM credential (broker 404 / missing key) is a typed 422 — fail-closed,
    never a silently-fabricated score."""
    from oraclous_knowledge_retriever_service.services.broker_client import BrokerError

    async def _boom(settings, *, credential_id, judge_model, organisation_id):  # noqa: ANN001
        raise BrokerError("credential cred-x not found")

    import oraclous_knowledge_retriever_service.routes.internal_routes as ir

    monkeypatch.setattr(ir, "resolve_byom_judge", _boom)
    resp = await async_client.post(
        "/internal/v1/evaluate", json=_body(judge_credential_id="cred-x"), headers=_AUTH
    )
    assert resp.status_code == 422
