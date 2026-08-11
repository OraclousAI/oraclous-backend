"""#743 — the three first-party retrieval connectors carry citations into the agent loop (§CITE).

Two things have to leave a retrieval connector, and they are NOT the same thing:

1. The ``citation`` of each hit stays inside the result CONTENT the model reads. #642 is the
   warning here: a receipt the model cannot see is a trap, and real models were failed for guessing
   at ids they were never shown. #742 already puts a typed ``citation`` on every retrieval hit, so
   the job here is that the connector passes it through untouched.
2. The connector emits ``served_citation_ids`` — a RESERVED result key, the ``data_absent`` (#580)
   pattern — carrying the ``citation_id`` of every cited hit in that result. The loop pops it, so it
   is platform state the model never sees and can never write.

The key is emitted only when the result actually cites something, exactly as ``data_absent`` is set
only on an empty result: a tool result carries no empty platform bookkeeping.

Covers acceptance criteria 1, 2 (connector half) and 8 of #743. The federated and find-similar
connectors are held to the identical behaviour — a federated answer that cannot be cited is not an
answer, and the key is the same key on the same envelope.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from oraclous_capability_registry_service.core.config import get_settings
from oraclous_capability_registry_service.domain.connectors.federated_search import (
    FederatedSearchConnector,
)
from oraclous_capability_registry_service.domain.connectors.find_similar import (
    FindSimilarConnector,
)
from oraclous_capability_registry_service.domain.connectors.knowledge_retriever import (
    KnowledgeRetrieverConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000007a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000007c5")
_GRAPH = "22222222-2222-2222-2222-222222222222"
_NODE = "33333333-3333-3333-3333-333333333333"

# Two real §CITE ids: "cit_" + 32 hex characters. Written out rather than computed, so the test
# pins the connector's extraction and not the identity function (which packages/citation owns).
_CIT_A = "cit_9f2a4c81b7d3e50a1c6f28934bd5e7a0"
_CIT_B = "cit_0b1d3f57a9c2e4680d8f1a3b5c7e9021"


def _citation(citation_id: str, title: str) -> dict[str, Any]:
    """A complete §CITE citation as the retriever serialises it onto a hit (#742)."""
    return {
        "citation_id": citation_id,
        "source_system": "github",
        "source_id": f"OraclousAI/oraclous-backend:{title}",
        "revision": "e3b0c44298fc1c149afbf4c8996fb924",
        "revision_kind": "blob_sha",
        "url": f"https://github.com/OraclousAI/oraclous-backend/blob/e3b0c44/{title}",
        "title": title,
        "author": {"display_name": "Parham Davari", "source_handle": "parhamdavari"},
        "retrieved_at": "2026-08-08T09:14:22Z",
        "permission_ref": None,
    }


def _hit(node_id: str, citation: dict[str, Any] | None) -> dict[str, Any]:
    return {"id": node_id, "type": "Chunk", "properties": {"text": "…"}, "citation": citation}


@pytest.fixture(autouse=True)
def _gateway_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "gateway")
    monkeypatch.setenv("KNOWLEDGE_RETRIEVER_URL", "http://knowledge-retriever-service:8000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
    )


def _responding(payload: Any) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


async def _retrieve(payload: Any) -> Any:
    ex = KnowledgeRetrieverConnector({"id": "x"})
    ex.transport = httpx.MockTransport(_responding(payload))
    return await ex.execute({"graph_id": _GRAPH, "query": "what did we agree"}, _ctx())


async def _federate(payload: Any) -> Any:
    ex = FederatedSearchConnector({"id": "x"})
    ex.transport = httpx.MockTransport(_responding(payload))
    return await ex.execute({"query": "what did we agree"}, _ctx())


async def _find_similar(payload: Any) -> Any:
    ex = FindSimilarConnector({"id": "x"})
    ex.transport = httpx.MockTransport(_responding(payload))
    return await ex.execute({"graph_id": _GRAPH, "node_id": _NODE}, _ctx())


# --- criterion 1: the citation stays where the model can read it ----------------------------


async def test_retriever_keeps_each_hit_citation_in_the_model_visible_content() -> None:
    # The model is asked to cite its sources, so it has to be SHOWN them (#642). The hit's typed
    # citation survives the connector unchanged — not summarised, not moved into metadata.
    res = await _retrieve([_hit("n1", _citation(_CIT_A, "partner-agreement.md"))])
    assert res.success
    assert res.data["hits"] == [_hit("n1", _citation(_CIT_A, "partner-agreement.md"))]


async def test_federated_keeps_each_result_citation_in_the_model_visible_content() -> None:
    result = {**_hit("n1", _citation(_CIT_A, "protocol-spec.md")), "source_graph_id": _GRAPH}
    res = await _federate({"results": [result], "meta": {}})
    assert res.success
    assert res.data["results"] == [result]


async def test_find_similar_keeps_each_hit_citation_in_the_model_visible_content() -> None:
    res = await _find_similar([_hit("n2", _citation(_CIT_B, "protocol-spec.md"))])
    assert res.success
    assert res.data["hits"] == [_hit("n2", _citation(_CIT_B, "protocol-spec.md"))]


# --- criterion 2 (connector half): the reserved served-ids key ------------------------------


async def test_retriever_emits_the_reserved_served_citation_ids_key() -> None:
    res = await _retrieve(
        [
            _hit("n1", _citation(_CIT_A, "partner-agreement.md")),
            _hit("n2", _citation(_CIT_B, "protocol-spec.md")),
        ]
    )
    assert res.success
    assert set(res.data["served_citation_ids"]) == {_CIT_A, _CIT_B}


async def test_retriever_deduplicates_ids_within_one_result() -> None:
    # citation_id is deterministic (§CITE), so two chunks of one document version share one id.
    # The same record served twice is ONE entry — the set is what the run served, not a call log.
    res = await _retrieve(
        [
            _hit("n1", _citation(_CIT_A, "partner-agreement.md")),
            _hit("n2", _citation(_CIT_A, "partner-agreement.md")),
        ]
    )
    assert res.success
    assert list(res.data["served_citation_ids"]) == [_CIT_A]


async def test_retriever_skips_hits_with_no_citation() -> None:
    # citation: null means the record has no source identity — ingested before §CITE, or ingested
    # with no source. It cannot be cited, so it contributes nothing and must not become a null id.
    res = await _retrieve([_hit("n1", None), _hit("n2", _citation(_CIT_B, "protocol-spec.md"))])
    assert res.success
    assert list(res.data["served_citation_ids"]) == [_CIT_B]


async def test_retriever_omits_the_key_when_nothing_cited() -> None:
    # The data_absent precedent: a reserved key is set only when it says something. A result where
    # no hit carries a citation carries no empty platform bookkeeping for the loop to pop.
    res = await _retrieve([_hit("n1", None)])
    assert res.success
    assert "served_citation_ids" not in res.data


async def test_empty_retrieval_still_flags_data_absent_and_serves_nothing() -> None:
    # #580 is unchanged by this issue: no hits is still data-absence, and it serves no citation.
    res = await _retrieve([])
    assert res.success
    assert res.data["data_absent"] is True
    assert "served_citation_ids" not in res.data


# --- criterion 8: the twins behave identically ----------------------------------------------


async def test_federated_emits_the_reserved_served_citation_ids_key() -> None:
    res = await _federate(
        {
            "results": [
                {
                    **_hit("n1", _citation(_CIT_A, "partner-agreement.md")),
                    "source_graph_id": _GRAPH,
                },
                {**_hit("n2", _citation(_CIT_B, "protocol-spec.md")), "source_graph_id": _GRAPH},
            ],
            "meta": {"graphs_searched": 2},
        }
    )
    assert res.success
    assert set(res.data["served_citation_ids"]) == {_CIT_A, _CIT_B}
    assert res.data["meta"] == {"graphs_searched": 2}


async def test_federated_deduplicates_the_same_document_across_graphs() -> None:
    # One document version mirrored into two workspaces is still ONE source: identity is keyed on
    # (source_system, source_id, revision), never on which graph the hit came from.
    res = await _federate(
        {
            "results": [
                {**_hit("n1", _citation(_CIT_A, "partner-agreement.md")), "source_graph_id": "g1"},
                {**_hit("n2", _citation(_CIT_A, "partner-agreement.md")), "source_graph_id": "g2"},
            ],
            "meta": {},
        }
    )
    assert res.success
    assert list(res.data["served_citation_ids"]) == [_CIT_A]


async def test_federated_omits_the_key_when_nothing_cited() -> None:
    res = await _federate({"results": [_hit("n1", None)], "meta": {}})
    assert res.success
    assert "served_citation_ids" not in res.data


async def test_find_similar_emits_the_reserved_served_citation_ids_key() -> None:
    res = await _find_similar(
        [
            _hit("n1", _citation(_CIT_A, "partner-agreement.md")),
            _hit("n2", _citation(_CIT_B, "protocol-spec.md")),
        ]
    )
    assert res.success
    assert set(res.data["served_citation_ids"]) == {_CIT_A, _CIT_B}


async def test_find_similar_omits_the_key_when_nothing_cited() -> None:
    res = await _find_similar([_hit("n1", None)])
    assert res.success
    assert "served_citation_ids" not in res.data
