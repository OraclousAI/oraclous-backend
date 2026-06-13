"""KRS evaluation HTTP layer (#331) — real route + dev-auth + fake retrieval/judge.

Auth (401), the response envelope, request bounds (422), the typed no-judge-key 422, unknown
graph (404), the no-computable-metrics 422, the typed capacity 429, and the cross-gateway 422
shape contract (#333: typed errors must be in the Pydantic LIST shape the gateway's leak-safe
#225 extractor relays) are real route behaviour. The real retrieval path is covered by
test_evaluation_substrate.py.
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest
from oraclous_knowledge_retriever_service.core.dependencies import (
    get_eval_judge,
    get_evaluation_service,
    get_retrieval_service,
)
from oraclous_knowledge_retriever_service.services import evaluation_service as ev
from oraclous_knowledge_retriever_service.services.evaluation_service import EvaluationService

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}

_BODY = {
    "question": "Who wrote the first computer program?",
    "answer": "Ada Lovelace wrote it.",
    "ground_truth": "Ada Lovelace wrote the first computer program.",
}


class _FakeRetrieval:
    def __init__(self, exists: bool = True) -> None:
        self._exists = exists

    async def graph_exists(self, *, graph_id: str) -> bool:
        return self._exists

    async def hybrid(self, *, graph_id: str, query: str, top_k: int) -> list[dict]:
        return [
            {
                "id": "4:x:1",
                "type": "Chunk",
                "properties": {"text": "ada lovelace wrote the first program", "rrf_score": 0.03},
            }
        ]


class _FakeJudge:
    _RESPONSES = {
        ev.CLAIMS_SYSTEM: '{"claims": ["ada lovelace wrote it"]}',
        ev.CLAIM_VERDICT_SYSTEM: '{"supported": true}',
        ev.RELEVANCE_SYSTEM: '{"score": 0.9}',
        ev.PRECISION_SYSTEM: '{"relevant": true}',
        ev.STATEMENTS_SYSTEM: '{"statements": ["ada lovelace wrote the first program"]}',
        ev.RECALL_VERDICT_SYSTEM: '{"attributable": true}',
    }

    async def complete_json(self, *, system: str, user: str) -> str:
        return self._RESPONSES[system]

    async def complete_text(self, *, system: str, user: str) -> str:
        return "Ada Lovelace wrote the first computer program."


@pytest.fixture
def client(app, async_client):
    app.dependency_overrides[get_retrieval_service] = lambda: _FakeRetrieval()
    app.dependency_overrides[get_eval_judge] = lambda: _FakeJudge()
    yield async_client
    app.dependency_overrides.clear()


async def test_evaluate_requires_auth(client) -> None:
    resp = await client.post(f"/v1/graph/{uuid.uuid4()}/evaluate", json=_BODY)
    assert resp.status_code == 401


async def test_evaluate_returns_the_full_envelope(client) -> None:
    gid = str(uuid.uuid4())
    resp = await client.post(f"/v1/graph/{gid}/evaluate", json=_BODY, headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {
        "graph_id",
        "question",
        "answer",
        "retrieved_contexts",
        "scores",
        "overall",
        "metrics_computed",
        "is_grounded",
        "warnings",
    }
    assert body["graph_id"] == gid
    assert body["question"] == _BODY["question"]
    assert body["answer"] == _BODY["answer"]
    assert set(body["scores"].keys()) == {
        "faithfulness",
        "answer_relevance",
        "context_precision",
        "context_recall",
    }
    assert body["scores"]["faithfulness"] == 1.0
    assert body["metrics_computed"] == [
        "faithfulness",
        "answer_relevance",
        "context_precision",
        "context_recall",
    ]
    assert body["is_grounded"] is True
    assert body["retrieved_contexts"][0]["content"] == "ada lovelace wrote the first program"


async def test_unknown_graph_is_404(app, async_client) -> None:
    app.dependency_overrides[get_retrieval_service] = lambda: _FakeRetrieval(exists=False)
    app.dependency_overrides[get_eval_judge] = lambda: _FakeJudge()
    try:
        resp = await async_client.post(
            f"/v1/graph/{uuid.uuid4()}/evaluate", json=_BODY, headers=_AUTH
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


async def test_empty_question_is_422(client) -> None:
    resp = await client.post(
        f"/v1/graph/{uuid.uuid4()}/evaluate", json={"question": ""}, headers=_AUTH
    )
    assert resp.status_code == 422


async def test_question_length_bound_is_422(client) -> None:
    resp = await client.post(
        f"/v1/graph/{uuid.uuid4()}/evaluate", json={"question": "x" * 10_001}, headers=_AUTH
    )
    assert resp.status_code == 422


async def test_all_unknown_metrics_is_422(client) -> None:
    resp = await client.post(
        f"/v1/graph/{uuid.uuid4()}/evaluate",
        json={"question": "q?", "metrics": ["bogus"]},
        headers=_AUTH,
    )
    assert resp.status_code == 422
    (item,) = resp.json()["detail"]
    assert item["loc"] == ["body", "metrics"]
    assert item["type"] == "no_valid_metrics"
    assert item["msg"] == "No valid metrics to compute."


async def test_explicit_empty_metrics_list_is_422(client) -> None:
    # metrics: [] means "nothing computable", not "give me the defaults" (#333)
    resp = await client.post(
        f"/v1/graph/{uuid.uuid4()}/evaluate",
        json={"question": "q?", "metrics": []},
        headers=_AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "no_valid_metrics"


async def test_empty_answer_is_rejected_at_the_dto(client) -> None:
    # "" must be rejected up front (min_length=1), never silently judged or ignored (#333)
    resp = await client.post(
        f"/v1/graph/{uuid.uuid4()}/evaluate",
        json={"question": "q?", "answer": ""},
        headers=_AUTH,
    )
    assert resp.status_code == 422
    assert any(item["loc"] == ["body", "answer"] for item in resp.json()["detail"])


async def test_overlong_metric_name_is_rejected_at_the_dto(client) -> None:
    resp = await client.post(
        f"/v1/graph/{uuid.uuid4()}/evaluate",
        json={"question": "q?", "metrics": ["x" * 65]},
        headers=_AUTH,
    )
    assert resp.status_code == 422


async def test_unconfigured_judge_is_a_typed_422(app, async_client) -> None:
    # The REAL get_eval_judge runs (no override) on an app whose lifespan built no judge —
    # exactly the no-KRS_OPENAI_API_KEY posture: an explicit eval endpoint must refuse with a
    # machine-readable error, never fake scores.
    app.dependency_overrides[get_retrieval_service] = lambda: _FakeRetrieval()
    try:
        resp = await async_client.post(
            f"/v1/graph/{uuid.uuid4()}/evaluate", json=_BODY, headers=_AUTH
        )
        assert resp.status_code == 422, resp.text
        (item,) = resp.json()["detail"]
        assert item["loc"] == ["eval"]
        assert item["type"] == "eval_judge_not_configured"
        assert "KRS_OPENAI_API_KEY" in item["msg"]
    finally:
        app.dependency_overrides.clear()


async def test_typed_422_bodies_match_the_gateway_extractor_contract(app, async_client) -> None:
    # The gateway's #225 extractor relays ONLY {"detail": [{"loc": [...], "type": "..."}]} where
    # loc parts are str/int and type is a machine token — both KRS typed 422s must fit that
    # shape so loc+type survive the edge as VALIDATION_FAILED details instead of collapsing to
    # the detail-free envelope (#333).
    app.dependency_overrides[get_retrieval_service] = lambda: _FakeRetrieval()
    try:
        no_judge = await async_client.post(
            f"/v1/graph/{uuid.uuid4()}/evaluate", json=_BODY, headers=_AUTH
        )
        app.dependency_overrides[get_eval_judge] = lambda: _FakeJudge()
        no_metrics = await async_client.post(
            f"/v1/graph/{uuid.uuid4()}/evaluate",
            json={"question": "q?", "metrics": []},
            headers=_AUTH,
        )
        for resp in (no_judge, no_metrics):
            assert resp.status_code == 422, resp.text
            detail = resp.json()["detail"]
            assert isinstance(detail, list) and detail
            for item in detail:
                assert isinstance(item, dict)
                assert isinstance(item["loc"], list) and item["loc"]
                assert all(isinstance(part, str | int) for part in item["loc"])
                # loc parts survive the extractor's field charset ([A-Za-z0-9_]) unmangled
                assert all(re.fullmatch(r"[A-Za-z0-9_]+", str(p)) for p in item["loc"])
                # the type is a clean token the extractor upper-cases into ^[A-Z][A-Z0-9_]*$
                assert re.fullmatch(r"[a-z][a-z0-9_]*", item["type"])
    finally:
        app.dependency_overrides.clear()


async def test_capacity_exhausted_is_a_typed_429(app, async_client) -> None:
    # the process-level evaluation slots are taken → a typed 429, not a queue-then-burn (#333)
    slots = asyncio.Semaphore(1)
    await slots.acquire()

    def _capped_service() -> EvaluationService:
        return EvaluationService(
            retrieval=_FakeRetrieval(),
            judge=_FakeJudge(),
            request_slots=slots,
            slot_wait_seconds=0.05,
        )

    app.dependency_overrides[get_evaluation_service] = _capped_service
    try:
        resp = await async_client.post(
            f"/v1/graph/{uuid.uuid4()}/evaluate", json=_BODY, headers=_AUTH
        )
        assert resp.status_code == 429, resp.text
        (item,) = resp.json()["detail"]
        assert item["loc"] == ["eval"]
        assert item["type"] == "eval_capacity_exceeded"
    finally:
        app.dependency_overrides.clear()
