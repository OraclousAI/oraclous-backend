"""Unit: the federated fan-out carries the query embedder's IDENTITY (#643 / #949 Q3).

Review finding on the #643 implementation: `FederatedRetrievalService` embedded the query with the
resolved embedder but called `RetrievalRepository.semantic()` without `embedder_id`, so the branch
filtered on the repository's old default — the legacy `hashing:512` space. Once the deployment
default flipped to a real embedder and a workspace had been re-embedded, `POST /v1/federated/search`
matched nothing, forever, with no error and no degradation flag: the exact silent-degrade class
#643 exists to close, on a live endpoint.

These tests pin the two halves of the fix. First, the identity actually reaches every semantic
branch (the fake repository RECORDS what it was asked for, so a regression to the default is a
failed assertion rather than an empty list nobody reads). Second, contributing nothing is never
silent: a partial mismatch degrades with the flag set, a total mismatch refuses.

The fakes here deliberately mirror the CURRENT repository contract — `semantic()` takes a required
`embedder_id`, and `has_any_chunk()` exists — which is what lets them catch a call site that
forgets either one.
"""

from __future__ import annotations

import pytest
from oraclous_embedding import LEGACY_NULL_EMBEDDER_ID
from oraclous_knowledge_retriever_service.services.federated_service import (
    FederatedRetrievalService,
)
from oraclous_knowledge_retriever_service.services.graph_registry_client import GraphInfo
from oraclous_knowledge_retriever_service.services.retrieval_service import (
    EmbedderIdentityMismatch,
)

pytestmark = pytest.mark.unit

_G1 = GraphInfo(id="11111111-1111-1111-1111-111111111111", name="research")
_G2 = GraphInfo(id="22222222-2222-2222-2222-222222222222", name="sales")

#: The identity a real deployment resolves — deliberately NOT the legacy default, so a branch that
#: silently falls back to the default fails these tests instead of passing by coincidence.
_OPENAI_ID = "openai:text-embedding-3-small:512"


class _IdentityRepo:
    """Records the `embedder_id` every semantic branch asked for, and serves rows per identity.

    `rows_by_graph` maps graph id -> the rows that graph holds UNDER `stored_identity`. A graph
    listed in `holds_chunks` has chunks but returns none for a foreign identity — the mismatch the
    existence probe is there to detect.
    """

    def __init__(
        self,
        rows_by_graph: dict[str, list[dict]],
        *,
        stored_identity: str = _OPENAI_ID,
        holds_chunks: set[str] | None = None,
    ) -> None:
        self.asked: list[tuple[str, str]] = []  # (graph_id, embedder_id)
        self.probed: list[str] = []
        self._rows = rows_by_graph
        self._stored = stored_identity
        self._holds = holds_chunks if holds_chunks is not None else set(rows_by_graph)

    def semantic(self, *, graph_id, qvec, top_k, embedder_id):
        self.asked.append((graph_id, embedder_id))
        if embedder_id != self._stored:
            return []  # the identity filter excludes every chunk in another space
        return list(self._rows.get(graph_id, []))[:top_k]

    def has_any_chunk(self, *, graph_id):
        self.probed.append(graph_id)
        return graph_id in self._holds

    def fulltext(self, *, graph_id, query, top_k):
        return list(self._rows.get(graph_id, []))[:top_k]

    def entity_search(self, *, graph_id, term, top_k):
        return list(self._rows.get(graph_id, []))[:top_k]


class _Registry:
    def __init__(self, graphs: list[GraphInfo]) -> None:
        self._graphs = graphs

    async def accessible_graphs(self, principal) -> list[GraphInfo]:
        return list(self._graphs)


class _Embedder:
    dim = 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _Service(FederatedRetrievalService):
    """Bypass the governance-bound repo builder with the injected fake."""

    def __init__(self, repo, graphs, *, embedder_id) -> None:
        super().__init__(
            driver=None,
            embedder=_Embedder(),
            registry=_Registry(graphs),
            embedder_id=embedder_id,
            max_graphs=20,
            max_per_graph_k=25,
            max_total=200,
            max_subgraph_nodes=500,
        )
        self._fake_repo = repo

    def _repo(self, organisation_id=None):
        return self._fake_repo


def _row(graph_id: str, ident: str, score: float = 0.9) -> dict:
    return {
        "id": ident,
        "labels": ["Chunk"],
        "props": {"name": ident, "graph_id": graph_id},
        "score": score,
    }


async def _search(svc, mode: str) -> dict:
    return await svc.search(
        principal=None, query="x", mode=mode, graph_ids=None, per_graph_k=5, total_k=50
    )


# ── the identity reaches every branch ────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
async def test_every_semantic_branch_is_filtered_by_the_resolved_identity(mode: str) -> None:
    """The regression itself: both the semantic mode's fan-out AND hybrid's semantic arm must ask
    for the identity the service resolved. Before the fix each asked for `hashing:512`."""
    repo = _IdentityRepo({_G1.id: [_row(_G1.id, "a")], _G2.id: [_row(_G2.id, "b")]})
    out = await _search(_Service(repo, [_G1, _G2], embedder_id=_OPENAI_ID), mode)

    assert repo.asked == [(_G1.id, _OPENAI_ID), (_G2.id, _OPENAI_ID)]
    assert {r["id"] for r in out["results"]} == {"a", "b"}
    assert out["meta"]["semantic_degraded"] is False


async def test_a_deployment_still_on_the_legacy_space_asks_for_the_legacy_identity() -> None:
    """The constant stays reachable when a caller genuinely MEANS the legacy space — it just can
    never be reached by omission."""
    repo = _IdentityRepo({_G1.id: [_row(_G1.id, "a")]}, stored_identity=LEGACY_NULL_EMBEDDER_ID)
    out = await _search(_Service(repo, [_G1], embedder_id=LEGACY_NULL_EMBEDDER_ID), "semantic")

    assert repo.asked == [(_G1.id, LEGACY_NULL_EMBEDDER_ID)]
    assert [r["id"] for r in out["results"]] == ["a"]


# ── contributing nothing is never silent ─────────────────────────────────────────────────────


async def test_a_total_identity_mismatch_refuses_instead_of_returning_an_empty_200() -> None:
    """Every queried graph holds chunks, none in this query's space. Before the fix this was a
    200 with an empty list — indistinguishable from "nothing matched your query"."""
    repo = _IdentityRepo(
        {_G1.id: [_row(_G1.id, "a")], _G2.id: [_row(_G2.id, "b")]},
        stored_identity=LEGACY_NULL_EMBEDDER_ID,  # the graphs are still in the old space
    )
    svc = _Service(repo, [_G1, _G2], embedder_id=_OPENAI_ID)

    with pytest.raises(EmbedderIdentityMismatch):
        await _search(svc, "semantic")


async def test_a_partial_mismatch_serves_the_healthy_graphs_and_flags_the_degradation() -> None:
    """One graph mid-re-embed must not sink a query the others can answer (ADR-026 partial
    result) — but the caller has to be told the semantic contribution was incomplete."""
    repo = _IdentityRepo({_G1.id: [_row(_G1.id, "a")]}, holds_chunks={_G1.id, _G2.id})
    out = await _search(_Service(repo, [_G1, _G2], embedder_id=_OPENAI_ID), "semantic")

    assert [r["id"] for r in out["results"]] == ["a"]  # the healthy graph still served
    assert out["meta"]["semantic_degraded"] is True  # …and the shortfall is visible
    assert repo.probed == [_G2.id]  # the probe ran ONLY for the branch that came back empty


async def test_hybrid_flags_the_degradation_rather_than_passing_off_fulltext_as_fused() -> None:
    """The worse half of the original bug: hybrid fused an empty semantic list with the fulltext
    list and returned what was effectively fulltext-only ranking under a `hybrid` label."""
    repo = _IdentityRepo({_G1.id: [_row(_G1.id, "a")]}, holds_chunks={_G1.id, _G2.id})
    out = await _search(_Service(repo, [_G1, _G2], embedder_id=_OPENAI_ID), "hybrid")

    assert out["meta"]["semantic_degraded"] is True
    assert out["results"], "hybrid still serves what the lexical arm found"


async def test_a_genuinely_empty_graph_is_not_reported_as_a_mismatch() -> None:
    """A graph holding nothing is an ordinary miss, not a refusal and not a degradation — or every
    empty federated search would look like a scary error."""
    repo = _IdentityRepo({}, holds_chunks=set())
    out = await _search(_Service(repo, [_G1, _G2], embedder_id=_OPENAI_ID), "semantic")

    assert out["results"] == []
    assert out["meta"]["semantic_degraded"] is False
