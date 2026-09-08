"""#949 ruling Q3 (C3, write half) — every stored `:Chunk` carries the identity + dimension of the
embedder that produced its vector.

`build_document_graph` (repositories/graph_write_repository.py) is pure (no I/O), so this is a
plain unit test. Today it takes `embeddings: list[list[float]]` and stamps only `id`/`text`/`index`
+ the `embedding` vector property; it has no notion of WHICH embedder produced those vectors. C3
adds an `embedder_id: str` parameter (the one shared `embedder_identity(settings)` string — the C1
contract test pins that both write and read call the same function) and stamps it, plus
`embedding_dim`, onto every `:Chunk` node — otherwise a workspace mid-#949-re-embed (C6) has no way
to tell which vectors are still in the old space, and the read side (C3's other half) has nothing
to filter on.

RED until the `[impl]` widens `build_document_graph`'s signature and stamps the two properties.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_GRAPH_ID = "11111111-1111-1111-1111-111111111111"


def _build(*, embedder_id: str, embedding_dim: int = 512):
    from oraclous_knowledge_graph_service.repositories.graph_write_repository import (  # noqa: PLC0415, E501
        build_document_graph,
    )

    return build_document_graph(
        graph_id=_GRAPH_ID,
        document="doc.txt",
        chunks=["first chunk", "second chunk"],
        embeddings=[[0.1] * embedding_dim, [0.2] * embedding_dim],
        embedder_id=embedder_id,
        embedding_dim=embedding_dim,
    )


def _chunk_nodes(graph):
    return [n for n in graph.nodes if n.label == "Chunk"]


def test_every_chunk_node_carries_the_embedder_id() -> None:
    graph = _build(embedder_id="hashing:512")
    chunks = _chunk_nodes(graph)
    assert len(chunks) == 2
    assert all(c.properties["embedder_id"] == "hashing:512" for c in chunks)


def test_every_chunk_node_carries_the_embedding_dim() -> None:
    graph = _build(embedder_id="hashing:512", embedding_dim=512)
    chunks = _chunk_nodes(graph)
    assert all(c.properties["embedding_dim"] == 512 for c in chunks)


def test_the_openai_identity_string_is_stamped_verbatim() -> None:
    graph = _build(embedder_id="openai:text-embedding-3-small:512")
    chunks = _chunk_nodes(graph)
    assert all(c.properties["embedder_id"] == "openai:text-embedding-3-small:512" for c in chunks)


def test_the_document_node_carries_no_embedder_id() -> None:
    """Only `:Chunk` nodes hold vectors; the identity is meaningless on the `:Document` node and
    stamping it there would be a second, easy-to-miss place a mismatch could hide."""
    graph = _build(embedder_id="hashing:512")
    (doc_node,) = [n for n in graph.nodes if n.label == "Document"]
    assert "embedder_id" not in doc_node.properties
    assert "embedding_dim" not in doc_node.properties


def test_chunk_node_ids_are_unaffected_by_the_new_properties() -> None:
    """The text does not change, so chunk ids must not change either — otherwise C6's re-embed
    task would silently break every existing reference to a chunk (relationships, citations) on
    its very first run. Deterministic ids are content/index-derived, never property-derived."""
    from oraclous_knowledge_graph_service.repositories.graph_write_repository import (  # noqa: PLC0415, E501
        chunk_node_ids,
    )

    expected = chunk_node_ids(graph_id=_GRAPH_ID, document="doc.txt", count=2)
    graph = _build(embedder_id="hashing:512")
    assert sorted(c.id for c in _chunk_nodes(graph)) == sorted(expected)

    graph_reembedded = _build(embedder_id="openai:text-embedding-3-small:512")
    assert sorted(c.id for c in _chunk_nodes(graph_reembedded)) == sorted(expected)
