"""Unit tests for KRS retrieval: embedder, NodeResult mapping, the service modes + RRF fusion
(driven through a fake Neo4j driver — no real DB), and org-context binding.
"""

from __future__ import annotations

import math
import uuid

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_retriever_service.services.embedder import HashingEmbedder
from oraclous_knowledge_retriever_service.services.retrieval_service import (
    RetrievalService,
    _to_node_result,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_ORG, principal_type=PrincipalType.USER
        )
    )


# --- embedder -----------------------------------------------------------------
def test_embedder_deterministic_dim_normalised() -> None:
    e = HashingEmbedder(dim=64)
    v1 = e.embed("ada lovelace")
    assert v1 == e.embed("ada lovelace")
    assert len(v1) == 64
    assert abs(math.sqrt(sum(x * x for x in v1)) - 1.0) < 1e-9


# --- NodeResult mapping -------------------------------------------------------
def test_node_result_strips_internal_labels_and_embedding() -> None:
    row = {
        "id": "4:abc:1",
        "labels": ["Chunk", "__KGBuilder__"],
        "props": {"text": "hi", "embedding": [0.1] * 512, "graph_id": "g"},
        "score": 0.87,
    }
    nr = _to_node_result(row)
    assert nr["id"] == "4:abc:1"
    assert nr["type"] == "Chunk"  # internal labels dropped
    assert "embedding" not in nr["properties"]  # vector never echoed
    assert nr["properties"]["score"] == 0.87
    assert nr["properties"]["text"] == "hi"


# --- service modes via a fake driver -----------------------------------------
class _FakeRecord:
    def __init__(self, d: dict) -> None:
        self._d = d

    def data(self) -> dict:
        return self._d


class _FakeDriver:
    def __init__(self, rows_by_call: list[list[dict]]) -> None:
        self._rows_by_call = rows_by_call
        self.calls = 0

    def execute_query(self, cypher, **kw):
        rows = self._rows_by_call[min(self.calls, len(self._rows_by_call) - 1)]
        self.calls += 1
        return ([_FakeRecord(r) for r in rows], None, None)


def _chunk_row(cid: str, text: str, score: float) -> dict:
    return {
        "id": cid,
        "labels": ["Chunk", "__KGBuilder__"],
        "props": {"text": text},
        "score": score,
    }


async def test_semantic_returns_node_results() -> None:
    driver = _FakeDriver([[_chunk_row("c1", "ada", 0.9), _chunk_row("c2", "babbage", 0.5)]])
    svc = RetrievalService(driver, HashingEmbedder(8))
    with _ctx():
        results = await svc.semantic(graph_id="g1", query="who wrote it", top_k=10)
    assert [r["type"] for r in results] == ["Chunk", "Chunk"]
    assert results[0]["properties"]["text"] == "ada"


async def test_hybrid_rrf_fuses_and_dedupes() -> None:
    # semantic returns c1,c2 ; fulltext returns c2,c3 -> c2 appears in both -> ranks highest
    driver = _FakeDriver(
        [
            [_chunk_row("c1", "a", 0.9), _chunk_row("c2", "b", 0.4)],  # semantic call
            [_chunk_row("c2", "b", 3.1), _chunk_row("c3", "c", 1.0)],  # fulltext call
        ]
    )
    svc = RetrievalService(driver, HashingEmbedder(8))
    with _ctx():
        results = await svc.hybrid(graph_id="g1", query="b", top_k=10)
    ids = [r["id"] for r in results]
    assert ids[0] == "c2"  # in both lists -> top RRF
    assert set(ids) == {"c1", "c2", "c3"}
    assert "rrf_score" in results[0]["properties"]


async def test_neighbors_carries_relationship() -> None:
    driver = _FakeDriver(
        [
            [
                {
                    "id": "n2",
                    "labels": ["Record"],
                    "props": {"name": "x"},
                    "relationship": "PART_OF",
                    "score": 1.0,
                }
            ]
        ]
    )
    svc = RetrievalService(driver, HashingEmbedder(8))
    with _ctx():
        results = await svc.neighbors(graph_id="g1", node_id="n1", top_k=10)
    assert results[0]["properties"]["relationship"] == "PART_OF"
