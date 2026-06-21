"""Real-Neo4j integration for the evaluation endpoint (#331 — real substrate).

A real Neo4j container is seeded with org-stamped :Chunk graphs; evaluation runs through the
REAL route → REAL RetrievalService → REAL repository Cypher (only the LLM judge is fake), so the
contexts the metrics judge against come from the actual hybrid read path. Cross-org isolation:
another organisation's graph is a 404 through the same path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from oraclous_knowledge_retriever_service.core.dependencies import (
    get_eval_judge,
    get_neo4j_driver,
)
from oraclous_knowledge_retriever_service.services import evaluation_service as ev
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_NEO4J_IMAGE = "neo4j:5.23-community"

# Must mirror Settings.dev_org_id — the org the dev-auth seam binds for `Bearer dev-token`.
_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_OTHER_ORG = "99999999-9999-9999-9999-999999999999"

_AUTH = {"Authorization": "Bearer dev-token"}

_TEXTS = (
    "ada lovelace wrote the first computer program",
    "charles babbage designed the analytical engine",
    "the jacquard loom inspired early computing machines",
)
_OTHER_ORG_TEXT = "other org confidential roadmap"


class _FakeJudge:
    _RESPONSES = {
        ev.CLAIMS_SYSTEM: '{"claims": ["ada lovelace wrote the first computer program"]}',
        ev.CLAIM_VERDICT_SYSTEM: '{"supported": true}',
        ev.RELEVANCE_SYSTEM: '{"score": 0.8}',
        ev.PRECISION_SYSTEM: '{"relevant": true}',
        ev.STATEMENTS_SYSTEM: '{"statements": ["ada lovelace wrote the first program"]}',
        ev.RECALL_VERDICT_SYSTEM: '{"attributable": true}',
    }

    async def complete_json(self, *, system: str, user: str) -> str:
        return self._RESPONSES[system]

    async def complete_text(self, *, system: str, user: str) -> str:
        return "Ada Lovelace wrote the first computer program."


@pytest.fixture(scope="module")
def real_neo4j_driver() -> Iterator[object]:
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer(_NEO4J_IMAGE).with_env("NEO4J_AUTH", "neo4j/password") as container:
        driver = GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", "password"))
        try:
            driver.verify_connectivity()
            yield driver
        finally:
            driver.close()


@pytest.fixture(scope="module")
def seeded_graphs(real_neo4j_driver) -> tuple[str, str]:
    """Seed one graph for the dev org and one for ANOTHER org, embeddings via the write-side
    hashing embedder (512) so the real semantic read path scores them."""
    embedder = HashingEmbedder(dim=512)
    graph_dev, graph_other = str(uuid.uuid4()), str(uuid.uuid4())
    for text in _TEXTS:
        real_neo4j_driver.execute_query(
            "CREATE (:Chunk {graph_id: $g, organisation_id: $o, text: $t, embedding: $e})",
            g=graph_dev,
            o=_DEV_ORG,
            t=text,
            e=embedder.embed(text),
        )
    real_neo4j_driver.execute_query(
        "CREATE (:Chunk {graph_id: $g, organisation_id: $o, text: $t, embedding: $e})",
        g=graph_other,
        o=_OTHER_ORG,
        t=_OTHER_ORG_TEXT,
        e=embedder.embed(_OTHER_ORG_TEXT),
    )
    return graph_dev, graph_other


@pytest.fixture
def client(app, async_client, real_neo4j_driver):
    # Real retrieval wiring against the container driver; ONLY the judge is fake.
    app.dependency_overrides[get_neo4j_driver] = lambda: real_neo4j_driver
    app.dependency_overrides[get_eval_judge] = lambda: _FakeJudge()
    yield async_client
    app.dependency_overrides.clear()


async def test_evaluate_through_the_real_retrieval_path(client, seeded_graphs) -> None:
    graph_dev, _ = seeded_graphs
    resp = await client.post(
        f"/v1/graph/{graph_dev}/evaluate",
        json={
            "question": "Who wrote the first computer program?",
            "ground_truth": "Ada Lovelace wrote the first computer program.",
        },
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The contexts are REAL — seeded chunk text fetched by the actual hybrid Cypher path.
    contents = [c["content"] for c in body["retrieved_contexts"]]
    assert contents, "real retrieval returned no contexts"
    assert any("ada lovelace" in c for c in contents)
    assert set(contents) <= set(_TEXTS)  # only THIS graph's chunks
    # The other org's data never leaks into the judged contexts.
    assert all(_OTHER_ORG_TEXT not in c for c in contents)
    # The answer was generated (none supplied) and all four metrics computed over real contexts.
    assert body["answer"] == "Ada Lovelace wrote the first computer program."
    assert body["metrics_computed"] == [
        "faithfulness",
        "answer_relevance",
        "context_precision",
        "context_recall",
    ]
    assert body["scores"]["faithfulness"] == 1.0
    assert body["scores"]["answer_relevance"] == 0.8
    assert body["overall"] is not None
    assert body["is_grounded"] is True


async def test_another_orgs_graph_is_404(client, seeded_graphs) -> None:
    _, graph_other = seeded_graphs
    resp = await client.post(
        f"/v1/graph/{graph_other}/evaluate",
        json={"question": "What is on the roadmap?"},
        headers=_AUTH,
    )
    assert resp.status_code == 404


async def test_nonexistent_graph_is_404(client, seeded_graphs) -> None:
    resp = await client.post(
        f"/v1/graph/{uuid.uuid4()}/evaluate",
        json={"question": "Anything?"},
        headers=_AUTH,
    )
    assert resp.status_code == 404
