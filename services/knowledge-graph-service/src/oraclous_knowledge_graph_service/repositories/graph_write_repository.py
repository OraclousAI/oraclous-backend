"""Neo4j write + schema repository (ORAA-4 §21 repositories layer — the only Neo4j driver access).

Builds a lexical neo4j_graphrag `Neo4jGraph` of one `:Document` + N `:Chunk` nodes (matching the
default `LexicalGraphConfig`, so the base writer treats them as lexical and adds no `:__Entity__`)
and writes it through the already-real `OrganisationScopedKGWriter`, which stamps organisation_id +
graph_id + bitemporal on every node/rel (fail-closed on the bound org context). Node ids are
deterministic sha256 over (graph_id, document, index); graph_id is a per-org UUID, so ids are
globally unique and re-ingest is idempotent (fixes the legacy per-document-index collision).

Reads (`schema`) are org+graph scoped with bound parameters (never interpolated): injection-safe.
"""

from __future__ import annotations

import hashlib

from neo4j import Driver
from neo4j_graphrag.experimental.components.kg_writer import Neo4jWriter
from neo4j_graphrag.experimental.components.types import (
    Neo4jGraph,
    Neo4jNode,
    Neo4jRelationship,
)
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.multi_tenant import OrganisationScopedKGWriter

_INTERNAL_LABEL_PREFIX = "__"


def _node_id(graph_id: str, document: str, index: int | None) -> str:
    suffix = "document" if index is None else f"chunk:{index}"
    payload = f"{graph_id}|{document}|{suffix}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_document_graph(
    *,
    graph_id: str,
    document: str,
    chunks: list[str],
    embeddings: list[list[float]],
    title: str | None = None,
) -> Neo4jGraph:
    """Build a lexical :Document + N :Chunk graph (FROM_DOCUMENT + NEXT_CHUNK).

    Pure (no I/O) so it is unit-testable; node ids are deterministic + globally unique
    (graph_id is a per-org UUID), so re-ingest is an idempotent MERGE.
    """
    doc_id = _node_id(graph_id, document, None)
    doc_node = Neo4jNode(
        id=doc_id,
        label="Document",
        properties={"id": doc_id, "name": document, "title": title or document},
    )
    chunk_nodes: list[Neo4jNode] = []
    for idx, (text, vector) in enumerate(zip(chunks, embeddings, strict=True)):
        cid = _node_id(graph_id, document, idx)
        chunk_nodes.append(
            Neo4jNode(
                id=cid,
                label="Chunk",
                properties={"id": cid, "text": text, "index": idx},
                embedding_properties={"embedding": vector},
            )
        )
    relationships: list[Neo4jRelationship] = [
        Neo4jRelationship(start_node_id=c.id, end_node_id=doc_id, type="FROM_DOCUMENT")
        for c in chunk_nodes
    ]
    for i in range(len(chunk_nodes) - 1):
        relationships.append(
            Neo4jRelationship(
                start_node_id=chunk_nodes[i].id,
                end_node_id=chunk_nodes[i + 1].id,
                type="NEXT_CHUNK",
            )
        )
    return Neo4jGraph(nodes=[doc_node, *chunk_nodes], relationships=relationships)


class WriteResult:
    """Counts returned from a document write."""

    def __init__(self, *, nodes: int, relationships: int, chunks: int) -> None:
        self.nodes = nodes
        self.relationships = relationships
        self.chunks = chunks


class GraphWriteRepository:
    """Writes lexical document graphs + reads org-scoped schema. Holds the Neo4j driver."""

    def __init__(self, driver: Driver, *, database: str | None = None) -> None:
        self._driver = driver
        self._database = database

    async def write_document(
        self,
        *,
        graph_id: str,
        document: str,
        chunks: list[str],
        embeddings: list[list[float]],
        title: str | None = None,
    ) -> WriteResult:
        # Replace-document semantics -> idempotent re-ingest. The neo4j_graphrag lexical writer does
        # not MERGE across runs (its __tmp_internal_id is transient), so we delete this document's
        # existing nodes (org + graph + ingestion_source scoped) before writing the fresh graph.
        self._delete_document(graph_id=graph_id, document=document)
        graph = build_document_graph(
            graph_id=graph_id,
            document=document,
            chunks=chunks,
            embeddings=embeddings,
            title=title,
        )
        base = Neo4jWriter(driver=self._driver, neo4j_database=self._database, clean_db=False)
        writer = OrganisationScopedKGWriter(
            base_writer=base, graph_id=graph_id, ingestion_source=document
        )
        # Org id is read live from the bound context inside run() (fail-closed).
        await writer.run(graph)
        n_chunks = len(graph.nodes) - 1
        return WriteResult(
            nodes=len(graph.nodes),
            relationships=len(graph.relationships),
            chunks=n_chunks,
        )

    def _delete_document(self, *, graph_id: str, document: str) -> None:
        """Detach-delete this document's nodes (org+graph+source scoped) for replace semantics."""
        source = document.replace("\x00", "").strip()
        self._driver.execute_query(
            "MATCH (n {graph_id: $graph_id}) "
            "WHERE n.organisation_id = $organisation_id AND n.ingestion_source = $source "
            "DETACH DELETE n",
            graph_id=graph_id,
            organisation_id=enforced_organisation_id(),
            source=source,
            database_=self._database,
        )

    def schema(self, *, graph_id: str, organisation_id: str) -> dict[str, list[dict[str, object]]]:
        """Org+graph-scoped label/relationship counts (bound params; sync driver call)."""
        label_records, _, _ = self._driver.execute_query(
            "MATCH (n {graph_id: $graph_id}) WHERE n.organisation_id = $organisation_id "
            "UNWIND labels(n) AS label "
            "RETURN label AS label, count(*) AS node_count ORDER BY label",
            graph_id=graph_id,
            organisation_id=organisation_id,
            database_=self._database,
        )
        rel_records, _, _ = self._driver.execute_query(
            "MATCH (s {graph_id: $graph_id})-[r]->(e {graph_id: $graph_id}) "
            "WHERE s.organisation_id = $organisation_id AND e.organisation_id = $organisation_id "
            "RETURN type(r) AS rel_type, count(r) AS rel_count ORDER BY rel_type",
            graph_id=graph_id,
            organisation_id=organisation_id,
            database_=self._database,
        )
        labels = [
            {"label": r["label"], "count": r["node_count"]}
            for r in label_records
            if not str(r["label"]).startswith(_INTERNAL_LABEL_PREFIX)
        ]
        relationships = [{"type": r["rel_type"], "count": r["rel_count"]} for r in rel_records]
        return {"labels": labels, "relationships": relationships}
