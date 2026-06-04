"""Unit tests for the S2 ingestion components (extractors, chunker, embedder, graph builder).

All deterministic and key-free — no Neo4j, no Postgres, no network.
"""

from __future__ import annotations

import math

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    build_document_graph,
)
from oraclous_knowledge_graph_service.services.chunker import chunk_text
from oraclous_knowledge_graph_service.services.embedder import (
    HashingEmbedder,
    make_embedder,
)
from oraclous_knowledge_graph_service.services.extractors import (
    ExtractionError,
    extract_text,
    source_type_for,
)

pytestmark = pytest.mark.unit


# --- extractors ---------------------------------------------------------------
def test_extract_plain_text() -> None:
    text, meta = extract_text(data=b"hello\n\nworld", filename="a.txt", source_type="text")
    assert "hello" in text and "world" in text
    assert meta["kind"] == "text"


def test_extract_markdown_structured() -> None:
    text, meta = extract_text(data=b"# Title\n\nbody text", filename="n.md", source_type="md")
    assert text.startswith("# Title")  # md text is the RAW markdown
    assert meta["structured"]["title"] == "Title"
    assert meta["structured"]["sections"][0]["heading"] == "Title"


def test_source_type_for() -> None:
    assert source_type_for("a.pdf") == "pdf"
    assert source_type_for("a.md") == "md"
    assert source_type_for("a.txt") == "text"
    assert source_type_for("README") == "text"


def test_unsupported_extension_raises() -> None:
    with pytest.raises(ExtractionError):
        source_type_for("a.exe")


def test_empty_text_raises() -> None:
    with pytest.raises(ExtractionError):
        extract_text(data=b"   \n\n  ", filename="a.txt", source_type="text")


# --- chunker ------------------------------------------------------------------
def test_chunk_splits_on_blank_lines() -> None:
    assert chunk_text("a\n\nb\n\n\n\nc") == ["a", "b", "c"]


def test_chunk_strips_and_drops_empty() -> None:
    assert chunk_text("  x  \n\n   \n\n y ") == ["x", "y"]


# --- embedder -----------------------------------------------------------------
def test_hashing_embedder_deterministic_and_dim() -> None:
    e = HashingEmbedder(dim=64)
    v1 = e.embed(["hello world"])
    v2 = e.embed(["hello world"])
    assert v1 == v2
    assert len(v1[0]) == 64


def test_hashing_embedder_l2_normalised() -> None:
    v = HashingEmbedder(dim=64).embed(["alpha beta gamma"])[0]
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-9


def test_hashing_embedder_empty_is_zero_vector() -> None:
    assert HashingEmbedder(dim=8).embed([""])[0] == [0.0] * 8


def test_make_embedder_default_is_hashing() -> None:
    assert isinstance(make_embedder(Settings()), HashingEmbedder)


def test_make_embedder_openai_requires_key() -> None:
    with pytest.raises(RuntimeError):
        make_embedder(Settings(embedder="openai", openai_api_key=None))


# --- graph builder ------------------------------------------------------------
def test_build_document_graph_shape() -> None:
    g = build_document_graph(
        graph_id="g1", document="d.txt", chunks=["a", "b"], embeddings=[[0.1] * 4, [0.2] * 4]
    )
    labels = [n.label for n in g.nodes]
    assert labels.count("Document") == 1
    assert labels.count("Chunk") == 2
    chunk0 = next(n for n in g.nodes if n.label == "Chunk" and n.properties["index"] == 0)
    assert chunk0.embedding_properties["embedding"] == [0.1] * 4
    assert chunk0.properties["text"] == "a"
    rel_types = sorted(r.type for r in g.relationships)
    assert rel_types == ["FROM_DOCUMENT", "FROM_DOCUMENT", "NEXT_CHUNK"]


def test_node_ids_deterministic_and_graph_scoped() -> None:
    a = build_document_graph(graph_id="g1", document="d", chunks=["x"], embeddings=[[0.0]])
    b = build_document_graph(graph_id="g1", document="d", chunks=["x"], embeddings=[[0.0]])
    assert [n.id for n in a.nodes] == [n.id for n in b.nodes]  # deterministic -> idempotent MERGE
    c = build_document_graph(graph_id="g2", document="d", chunks=["x"], embeddings=[[0.0]])
    assert a.nodes[0].id != c.nodes[0].id  # different graph -> different ids (no collision)
