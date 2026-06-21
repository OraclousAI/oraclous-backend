"""Ingestion use-case (services layer) — extract → chunk → embed → (extract) → write.

The text/document spine: turn raw bytes into a `:Document`/`:Chunk` graph via the org-scoped writer.

Two modes, selected by `KGS_EXTRACTOR`:
  - `null` (default, key-free CI): lexical only — `:Document` + `:Chunk` with deterministic
    embeddings. No LLM, no entities. `extractor` is None.
  - `openai`: additionally runs LLM entity + relationship extraction over the chunks, so the same
    free text also materialises real domain `:Entity` nodes + their relationships, linked
    (`FROM_CHUNK`) to the chunk they came from. `extractor` is an injectable `EntityExtractor`.

Slice B: when the graph carries a TYPED ontology, the extractor is built with a hard `GraphSchema`
(so the LLM extracts conforming types) AND, post-extraction, the SAME strict/coerce enforcement the
structured path uses is applied here too (`resolve_label`): a strict graph drops an off-ontology
entity (and its incident edges) before the write; a coerce graph remaps a near-match label. So
free-text ingestion can never add an off-ontology node to a strict graph.

The org scope is bound by the caller (HTTP request or the worker's `use_organisation_context`); the
write repository stamps `organisation_id` on every node/rel (an LLM-extracted org id cannot
redirect a write to another tenant).
"""

from __future__ import annotations

from dataclasses import dataclass

from neo4j_graphrag.experimental.components.types import Neo4jGraph

from oraclous_knowledge_graph_service.domain.ontology import Ontology, resolve_label
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
    WriteResult,
    chunk_node_ids,
)
from oraclous_knowledge_graph_service.services.chunker import chunk_text
from oraclous_knowledge_graph_service.services.embedder import Embedder
from oraclous_knowledge_graph_service.services.entity_extractor import EntityExtractor
from oraclous_knowledge_graph_service.services.extractors import ExtractionError, extract_text


class IngestionError(Exception):
    """Ingestion failed (no extractable text, or a downstream write error)."""


@dataclass
class _Enforced:
    """The ontology-enforced entity sub-graph plus the strict-drop / coerce-remap counts."""

    graph: Neo4jGraph
    violations: int
    coercions: int


def enforce_ontology(entity_graph: Neo4jGraph, ontology: Ontology | None) -> _Enforced:
    """Apply the graph's ontology to an extracted entity sub-graph (pure; reuses `resolve_label`).

    - open / None: passthrough (no change).
    - strict: drop every entity whose label is off-ontology, and any relationship incident to a
      dropped node (so no dangling edge survives). Counts each drop as a violation.
    - coerce: remap a near-match label to its allowed form (counted as a coercion); a too-far label
      is dropped (counted as a violation), like strict.
    """
    if ontology is None or ontology.mode == "open" or not ontology.allowed_labels:
        return _Enforced(entity_graph, 0, 0)

    kept_nodes = []
    dropped_ids: set[str] = set()
    violations = 0
    coercions = 0
    for node in entity_graph.nodes:
        resolved, coerced = resolve_label(ontology, node.label)
        if resolved is None:
            dropped_ids.add(node.id)
            violations += 1
            continue
        if coerced:
            node.label = resolved
            coercions += 1
        kept_nodes.append(node)

    kept_rels = [
        rel
        for rel in entity_graph.relationships
        if rel.start_node_id not in dropped_ids and rel.end_node_id not in dropped_ids
    ]
    return _Enforced(Neo4jGraph(nodes=kept_nodes, relationships=kept_rels), violations, coercions)


class IngestionService:
    def __init__(
        self,
        write_repo: GraphWriteRepository,
        embedder: Embedder,
        extractor: EntityExtractor | None = None,
        ontology: Ontology | None = None,
    ) -> None:
        self._write_repo = write_repo
        self._embedder = embedder
        self._extractor = extractor
        self._ontology = ontology

    async def ingest(
        self, *, graph_id: str, document: str, data: bytes, source_type: str | None
    ) -> WriteResult:
        try:
            text, _meta = extract_text(data=data, filename=document, source_type=source_type)
        except ExtractionError as exc:
            raise IngestionError(str(exc)) from exc
        chunks = chunk_text(text)
        if not chunks:
            raise IngestionError("no chunks produced from extracted text")
        embeddings = self._embedder.embed(chunks)
        entity_graph = None
        violations = 0
        coercions = 0
        if self._extractor is not None:
            # Link extracted entities to the SAME deterministic chunk ids the writer builds.
            chunk_ids = chunk_node_ids(graph_id=graph_id, document=document, count=len(chunks))
            extracted = await self._extractor.extract(chunks=chunks, chunk_ids=chunk_ids)
            # Hard schema steers the LLM; this strict/coerce pass is the belt-and-braces guarantee
            # that a strict graph never gains an off-ontology node from free text.
            enforced = enforce_ontology(extracted, self._ontology)
            entity_graph = enforced.graph
            violations = enforced.violations
            coercions = enforced.coercions
        return await self._write_repo.write_document(
            graph_id=graph_id,
            document=document,
            chunks=chunks,
            embeddings=embeddings,
            entity_graph=entity_graph,
            ontology_violations=violations,
            ontology_coercions=coercions,
        )
