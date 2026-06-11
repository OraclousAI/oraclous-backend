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
    LexicalGraphConfig,
    Neo4jGraph,
    Neo4jNode,
    Neo4jRelationship,
)
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.multi_tenant import OrganisationScopedKGWriter

_INTERNAL_LABEL_PREFIX = "__"
# entity → chunk edge type (matches the extractor's link type), excluded from the entity-relation
# count so `extracted_relationships` reports only entity↔entity edges, not the chunk attachments.
_FROM_CHUNK = LexicalGraphConfig().node_to_chunk_relationship_type


def _node_id(graph_id: str, document: str, index: int | None) -> str:
    suffix = "document" if index is None else f"chunk:{index}"
    payload = f"{graph_id}|{document}|{suffix}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def chunk_node_ids(*, graph_id: str, document: str, count: int) -> list[str]:
    """Deterministic ids for the `count` chunk nodes of a document.

    The single source of truth for chunk ids — the LLM extractor links its entities to these exact
    ids so the entity graph attaches to the lexical chunk nodes the writer creates.
    """
    return [_node_id(graph_id, document, idx) for idx in range(count)]


def build_document_graph(
    *,
    graph_id: str,
    document: str,
    chunks: list[str],
    embeddings: list[list[float]],
    title: str | None = None,
    entity_graph: Neo4jGraph | None = None,
) -> Neo4jGraph:
    """Build a lexical :Document + N :Chunk graph (FROM_DOCUMENT + NEXT_CHUNK).

    Pure (no I/O) so it is unit-testable; node ids are deterministic + globally unique
    (graph_id is a per-org UUID), so re-ingest is an idempotent MERGE.

    When `entity_graph` is given (LLM extraction on, `KGS_EXTRACTOR=openai`), its extracted entity
    nodes + entity↔entity relationships + entity→chunk (`FROM_CHUNK`) edges are merged in. The
    extractor links entities to the same deterministic chunk ids built here, so the entity graph
    attaches to these chunk nodes.
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
    nodes = [doc_node, *chunk_nodes]
    if entity_graph is not None:
        nodes.extend(entity_graph.nodes)
        relationships.extend(entity_graph.relationships)
    return Neo4jGraph(nodes=nodes, relationships=relationships)


class WriteResult:
    """Counts returned from a document write.

    `nodes`/`relationships` are the lexical+entity graph totals (Document + Chunks + entities; all
    edges). `entities`/`entity_relationships` are the LLM-extracted counts ONLY — the honest figures
    the ingest job reports as `extracted_entities`/`extracted_relationships` (0 in `null` mode,
    where the only nodes are the lexical Document + Chunks).
    """

    def __init__(
        self,
        *,
        nodes: int,
        relationships: int,
        chunks: int,
        entities: int = 0,
        entity_relationships: int = 0,
        ontology_violations: int = 0,
        ontology_coercions: int = 0,
    ) -> None:
        self.nodes = nodes
        self.relationships = relationships
        self.chunks = chunks
        self.entities = entities
        self.entity_relationships = entity_relationships
        # Free-text ontology enforcement (Slice B): off-type entities dropped (strict) / remapped
        # (coerce) before write — mirrors the structured path's ExecutionResult counts.
        self.ontology_violations = ontology_violations
        self.ontology_coercions = ontology_coercions


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
        entity_graph: Neo4jGraph | None = None,
        ontology_violations: int = 0,
        ontology_coercions: int = 0,
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
            entity_graph=entity_graph,
        )
        base = Neo4jWriter(driver=self._driver, neo4j_database=self._database, clean_db=False)
        writer = OrganisationScopedKGWriter(
            base_writer=base, graph_id=graph_id, ingestion_source=document
        )
        # Org id is read live from the bound context inside run() (fail-closed).
        await writer.run(graph)
        # Honest extracted counts: the entity nodes (FROM_CHUNK edges link them to chunks) and the
        # entity↔entity relationships the LLM produced — independent of the lexical Document/Chunk
        # scaffold. 0 in null mode (entity_graph is None).
        entity_count = len(entity_graph.nodes) if entity_graph else 0
        entity_rel_count = (
            sum(1 for r in entity_graph.relationships if r.type != _FROM_CHUNK)
            if entity_graph
            else 0
        )
        n_chunks = len(chunks)
        return WriteResult(
            nodes=len(graph.nodes),
            relationships=len(graph.relationships),
            chunks=n_chunks,
            entities=entity_count,
            entity_relationships=entity_rel_count,
            ontology_violations=ontology_violations,
            ontology_coercions=ontology_coercions,
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

    def delete_graph_nodes(self, *, graph_id: str) -> int:
        """Detach-delete every Neo4j node carrying this graph_id (cascade on graph delete).

        Graph-delete removes the Postgres metadata row; without this the graph's Neo4j
        nodes/edges are orphaned (storage leak + stale-node collisions on a re-create that
        reuses the id). All mapped nodes stamp `graph_id`, so a single graph_id-scoped
        DETACH DELETE clears the whole graph (the deterministic ids are sha256(graph_id|...),
        so they never resurface). Bound parameter (never interpolated): injection-safe. Sync
        driver call — callers in async code wrap it in `asyncio.to_thread`. Returns the node
        count deleted (for logging / surfacing).
        """
        records, _, _ = self._driver.execute_query(
            "MATCH (n {graph_id: $graph_id}) "
            "WITH collect(n) AS nodes "
            "WITH nodes, size(nodes) AS deleted "
            "FOREACH (n IN nodes | DETACH DELETE n) "
            "RETURN deleted",
            graph_id=graph_id,
            database_=self._database,
        )
        return int(records[0]["deleted"]) if records else 0

    def count_for_graph(self, *, graph_id: str, organisation_id: str) -> tuple[int, int]:
        """Live org+graph-scoped (node_count, relationship_count) from Neo4j (bound params; sync).

        The stale `node_count`/`relationship_count` Postgres columns are never updated by ingestion
        (real nodes land in Neo4j), so the GraphResponse must reflect these live counts. ALL graph
        nodes are counted: the unified model carries `__KGBuilder__`/`__Entity__` bookkeeping labels
        on every real node (Source/Document/Chunk/Entity/Table/...), so a `__`-prefix exclusion
        would wrongly drop the entire graph.
        """
        node_records, _, _ = self._driver.execute_query(
            "MATCH (n {graph_id: $graph_id}) WHERE n.organisation_id = $organisation_id "
            "RETURN count(n) AS c",
            graph_id=graph_id,
            organisation_id=organisation_id,
            database_=self._database,
        )
        rel_records, _, _ = self._driver.execute_query(
            "MATCH (s {graph_id: $graph_id})-[r]->(e {graph_id: $graph_id}) "
            "WHERE s.organisation_id = $organisation_id AND e.organisation_id = $organisation_id "
            "RETURN count(r) AS c",
            graph_id=graph_id,
            organisation_id=organisation_id,
            database_=self._database,
        )
        return int(node_records[0]["c"]), int(rel_records[0]["c"])

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
