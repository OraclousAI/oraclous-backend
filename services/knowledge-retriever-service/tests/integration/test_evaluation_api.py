"""KRS evaluation HTTP layer (#331) — real route + dev-auth + fake retrieval/judge.

Auth (401), the response envelope, request bounds (422), the typed no-judge-key 422, unknown
graph (404), and the no-computable-metrics 422 are real route behaviour. The real retrieval path
is covered by test_evaluation_substrate.py.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_knowledge_retriever_service.core.config import get_settings
from oraclous_knowledge_retriever_service.core.dependencies import (
    get_eval_judge,
    get_retrieval_service,
)
from oraclous_knowledge_retriever_service.services import evaluation_service as ev

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
    assert resp.json()["detail"] == "No valid metrics to compute."


async def test_no_judge_key_is_a_typed_422(app, async_client, monkeypatch) -> None:
    # The REAL get_eval_judge runs (no override) against settings with no KRS_OPENAI_API_KEY:
    # an explicit eval endpoint must refuse with a machine-readable error, never fake scores.
    monkeypatch.delenv("KRS_OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    app.dependency_overrides[get_retrieval_service] = lambda: _FakeRetrieval()
    try:
        resp = await async_client.post(
            f"/v1/graph/{uuid.uuid4()}/evaluate", json=_BODY, headers=_AUTH
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "eval_judge_not_configured"
        assert "KRS_OPENAI_API_KEY" in detail["message"]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
