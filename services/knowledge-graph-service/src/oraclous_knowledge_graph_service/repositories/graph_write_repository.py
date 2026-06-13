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
import re

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
# Relationship types re-pointed during a HITL merge are read back from the graph (a closed
# ontology), but re-validated against this allowlist before Cypher interpolation — defense in depth
# at the write boundary (mirrors recipe_write_repository._safe).
_SAFE_REL_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_rel_type(rel_type: str) -> str:
    if not isinstance(rel_type, str) or not _SAFE_REL_TYPE.match(rel_type):
        raise ValueError(f"unsafe relationship type at write boundary: {rel_type!r}")
    return rel_type


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

    # --- HITL entity-resolution mutations (#279) ----------------------------------------------
    # The resolution pass (#269) MERGEs a SAME_AS_CANDIDATE edge between two canonical :__Entity__
    # nodes in the ambiguous similarity band (flagged, not auto-merged). These methods action a
    # human verdict on such a pair. All are org+graph scoped with bound parameters (injection-safe);
    # sync driver calls (the async service wraps each in asyncio.to_thread). SAME_AS_CANDIDATE is
    # interpolated as a constant (a fixed identifier from the resolver, never user input).

    def candidate_endpoints(
        self, *, graph_id: str, organisation_id: str, node_id_a: str, node_id_b: str
    ) -> dict | None:
        """Resolve a live SAME_AS_CANDIDATE pair to its endpoints' ids + aliases, regardless of edge
        direction (the edge is undirected for review). None if no such pending candidate exists
        (already resolved, never flagged, wrong org/graph) — the service maps that to 404.
        """
        records, _, _ = self._driver.execute_query(
            "MATCH (a:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
            "id: $node_id_a}) "
            "MATCH (b:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
            "id: $node_id_b}) "
            "MATCH (a)-[:SAME_AS_CANDIDATE]-(b) "
            "RETURN a.id AS id_a, b.id AS id_b, labels(a) AS labels_a, labels(b) AS labels_b, "
            "coalesce(a.aliases, []) AS aliases_a, coalesce(b.aliases, []) AS aliases_b, "
            "coalesce(a.canonical_name, a.name) AS name_a, "
            "coalesce(b.canonical_name, b.name) AS name_b",
            graph_id=graph_id,
            organisation_id=organisation_id,
            node_id_a=node_id_a,
            node_id_b=node_id_b,
            database_=self._database,
        )
        if not records:
            return None
        r = records[0]
        return {
            "id_a": r["id_a"],
            "id_b": r["id_b"],
            "labels_a": list(r["labels_a"]),
            "labels_b": list(r["labels_b"]),
            "aliases_a": list(r["aliases_a"]),
            "aliases_b": list(r["aliases_b"]),
            "name_a": r["name_a"],
            "name_b": r["name_b"],
        }

    def merge_candidate(
        self, *, graph_id: str, organisation_id: str, survivor_id: str, merged_id: str
    ) -> dict:
        """Approve a candidate: fold `merged_id` onto `survivor_id`, then delete the merged node.

        Re-points every relationship of the merged node onto the survivor (pure Cypher, no APOC):
        each incoming/outgoing edge is recreated on the survivor with its type + properties,
        skipping a self-loop the merge would create and the SAME_AS_CANDIDATE edge itself (it is
        being resolved away). The survivor's `aliases` absorb the merged node's surface forms +
        name, then the merged node is DETACH DELETEd (removing the candidate edge). Returns the
        survivor id, the count of edges re-pointed, and the survivor's post-merge alias set.

        Idempotent at the graph level: once the merged node is gone, a replay finds no candidate
        edge (the service short-circuits on the audit row first; this is the substrate backstop).
        """
        # 1. Re-point the merged node's relationships onto the survivor (pure Cypher, no APOC).
        repointed = self._repoint_edges(
            graph_id=graph_id,
            organisation_id=organisation_id,
            survivor_id=survivor_id,
            merged_id=merged_id,
        )
        # 2. Union the merged node's aliases + names into the survivor, then DETACH DELETE the
        #    merged node (which removes the SAME_AS_CANDIDATE edge and any residual self-loops).
        records, _, _ = self._driver.execute_query(
            "MATCH (m:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
            "id: $merged_id}) "
            "MATCH (s:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
            "id: $survivor_id}) "
            "WITH s, m, coalesce(m.aliases, []) + "
            "[n IN [m.canonical_name, m.name] WHERE n IS NOT NULL] AS incoming "
            "SET s.aliases = coalesce(s.aliases, []) + "
            "[a IN incoming WHERE NOT a IN coalesce(s.aliases, [])] "
            "DETACH DELETE m "
            "RETURN s.id AS survivor_id, coalesce(s.aliases, []) AS aliases",
            graph_id=graph_id,
            organisation_id=organisation_id,
            merged_id=merged_id,
            survivor_id=survivor_id,
            database_=self._database,
        )
        row = records[0] if records else {}
        return {
            "survivor_id": row.get("survivor_id", survivor_id),
            "repointed_edges": repointed,
            "aliases": list(row.get("aliases", [])),
        }

    def _repoint_edges(
        self, *, graph_id: str, organisation_id: str, survivor_id: str, merged_id: str
    ) -> int:
        """Recreate the merged node's edges on the survivor (no APOC): one MERGE per edge carrying
        the original properties, preserving direction. Excludes the SAME_AS_CANDIDATE edge being
        resolved and any edge whose other endpoint is the survivor (would become a self-loop).
        Returns the number of edges re-pointed. The relationship type comes from the graph (a closed
        ontology); it is re-validated against the safe-identifier allowlist before interpolation.
        """
        # Collect the merged node's edges (type, direction, the other endpoint id, properties).
        records, _, _ = self._driver.execute_query(
            "MATCH (m:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
            "id: $merged_id})-[r]-(o) "
            "WHERE type(r) <> 'SAME_AS_CANDIDATE' AND o.id <> $survivor_id "
            "RETURN type(r) AS rel_type, "
            "startNode(r).id = $merged_id AS outgoing, o.id AS other_id, properties(r) AS props",
            graph_id=graph_id,
            organisation_id=organisation_id,
            merged_id=merged_id,
            survivor_id=survivor_id,
            database_=self._database,
        )
        repointed = 0
        for rec in records:
            rel_type = _safe_rel_type(rec["rel_type"])
            props = dict(rec["props"] or {})
            props["organisation_id"] = organisation_id
            props["graph_id"] = graph_id
            if rec["outgoing"]:
                cypher = (
                    "MATCH (s:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
                    "id: $survivor_id}) "
                    "MATCH (o:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
                    "id: $other_id}) "
                    f"MERGE (s)-[r:{rel_type} "
                    "{graph_id: $graph_id, organisation_id: $organisation_id}]->(o) "
                    "SET r += $props"
                )
            else:
                cypher = (
                    "MATCH (s:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
                    "id: $survivor_id}) "
                    "MATCH (o:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
                    "id: $other_id}) "
                    f"MERGE (o)-[r:{rel_type} "
                    "{graph_id: $graph_id, organisation_id: $organisation_id}]->(s) "
                    "SET r += $props"
                )
            self._driver.execute_query(
                cypher,
                graph_id=graph_id,
                organisation_id=organisation_id,
                survivor_id=survivor_id,
                other_id=rec["other_id"],
                props=props,
                database_=self._database,
            )
            repointed += 1
        return repointed

    def suppress_candidate(
        self, *, graph_id: str, organisation_id: str, node_id_a: str, node_id_b: str
    ) -> bool:
        """Reject a candidate: record a NOT_SAME_AS negative judgement between the two nodes and
        drop the SAME_AS_CANDIDATE edge so the pair leaves the review queue and the resolution pass
        does not re-flag it (the candidate-write step skips NOT_SAME_AS-marked pairs). Idempotent —
        a replay re-MERGEs the same NOT_SAME_AS and finds no candidate edge to delete. Returns True
        when a NOT_SAME_AS suppression edge is in place afterwards."""
        records, _, _ = self._driver.execute_query(
            "MATCH (a:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
            "id: $node_id_a}) "
            "MATCH (b:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, "
            "id: $node_id_b}) "
            "MERGE (a)-[s:NOT_SAME_AS "
            "{graph_id: $graph_id, organisation_id: $organisation_id}]-(b) "
            "WITH a, b "
            "OPTIONAL MATCH (a)-[c:SAME_AS_CANDIDATE]-(b) "
            "DELETE c "
            "RETURN true AS suppressed",
            graph_id=graph_id,
            organisation_id=organisation_id,
            node_id_a=node_id_a,
            node_id_b=node_id_b,
            database_=self._database,
        )
        return bool(records and records[0]["suppressed"])

    # --- cross-graph SAME_AS candidates (#330 / ADR-026) --------------------------------------
    # Cross-graph candidate generation folds into the SAME HITL pipeline (#279): a
    # SAME_AS_CANDIDATE edge between two canonical :__Entity__ nodes in two DIFFERENT graphs of
    # ONE org, BOTH graph ids carried on the edge. The verdicts reuse the same audit + endpoints;
    # an approve LINKS (MERGE SAME_AS) instead of folding — a merge would move nodes/edges across
    # graph boundaries, which a read-side federation must never cause. All queries bind org +
    # BOTH graph ids (a cross-ORG pair is unmatchable by construction).

    def cross_graph_entities(
        self, *, graph_id: str, organisation_id: str, limit: int
    ) -> list[dict]:
        """The canonical entities of ONE org-owned graph, shaped for cross-graph candidate
        generation: deterministic id, canonical key (`name`), display name, primary label."""
        records, _, _ = self._driver.execute_query(
            "MATCH (e:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id}) "
            "WHERE e.name IS NOT NULL "
            "RETURN e.id AS id, e.name AS name, "
            "coalesce(e.canonical_name, e.name) AS canonical_name, "
            "head([l IN labels(e) WHERE NOT l STARTS WITH '__']) AS label "
            # Deterministic ORDER BY before LIMIT: the scan is a bounded slice, so without an order
            # the `limit` truncation is run-to-run nondeterministic (which entities a re-generation
            # considers would drift). e.id is the deterministic node id — a stable boundary.
            "ORDER BY e.id LIMIT $limit",
            graph_id=graph_id,
            organisation_id=organisation_id,
            limit=limit,
            database_=self._database,
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "canonical_name": r["canonical_name"],
                "label": r["label"] or "Entity",
            }
            for r in records
        ]

    def write_cross_graph_candidates(self, *, organisation_id: str, pairs: list[dict]) -> int:
        """MERGE a SAME_AS_CANDIDATE edge per pair, BOTH endpoints org-scoped and each bound to
        its OWN graph id (a pair naming another org's node simply does not match — fail-closed).
        Pairs already human-resolved are skipped: a NOT_SAME_AS (reject) or SAME_AS (approve)
        edge suppresses re-flagging, mirroring the in-graph resolution pass. Returns the number
        of candidate edges present after the write. Each pair dict carries
        ``id_a/graph_id_a/id_b/graph_id_b/score/method``."""
        # Canonicalise the edge DIRECTION by node id before MERGE: a SAME_AS_CANDIDATE is undirected
        # for review, but a directed MERGE `(a)->(b)` and `(b)->(a)` are two distinct edges — so a
        # re-generation from the reversed direction wrote a DUPLICATE. MERGE always from the
        # lexicographically-smaller endpoint to the larger (`lo`->`hi`), independent of which side
        # the pair named `a`/`b`, so `(a,b)` and `(b,a)` collapse to ONE edge. The endpoints still
        # carry their own graph ids (a cross-org pair is unmatchable — fail-closed). The undirected
        # NOT_SAME_AS / SAME_AS guards are unchanged (already direction-insensitive).
        records, _, _ = self._driver.execute_query(
            "UNWIND $pairs AS pair "
            "MATCH (a:__Entity__ {organisation_id: $organisation_id, "
            "graph_id: pair.graph_id_a, id: pair.id_a}) "
            "MATCH (b:__Entity__ {organisation_id: $organisation_id, "
            "graph_id: pair.graph_id_b, id: pair.id_b}) "
            "WHERE NOT (a)-[:NOT_SAME_AS]-(b) AND NOT (a)-[:SAME_AS]-(b) "
            "WITH pair, a, b, "
            "(CASE WHEN a.id <= b.id THEN a ELSE b END) AS lo, "
            "(CASE WHEN a.id <= b.id THEN b ELSE a END) AS hi "
            "MERGE (lo)-[c:SAME_AS_CANDIDATE]->(hi) "
            "SET c.organisation_id = $organisation_id, "
            "c.graph_id_a = pair.graph_id_a, c.graph_id_b = pair.graph_id_b, "
            "c.score = pair.score, c.method = pair.method, c.cross_graph = true "
            "RETURN count(c) AS written",
            organisation_id=organisation_id,
            pairs=pairs,
            database_=self._database,
        )
        return int(records[0]["written"]) if records else 0

    def verdicted_cross_graph_pairs(
        self, *, organisation_id: str, graph_id_a: str, graph_id_b: str
    ) -> list[tuple[str, str]]:
        """The node-id pairs ACROSS the two org-owned graphs a human has already resolved — i.e.
        endpoints joined by a `SAME_AS` (approved link) or `NOT_SAME_AS` (rejected) edge. Returned
        as canonicalised `(lo, hi)` id tuples so the caller can drop already-verdicted pairs from a
        re-generation BEFORE spending the candidate-limit budget, and not over-count `generated`.
        Org-scoped + bound to BOTH graph ids on each endpoint (a cross-org pair is unmatchable)."""
        records, _, _ = self._driver.execute_query(
            "MATCH (a:__Entity__ {graph_id: $graph_id_a, organisation_id: $organisation_id}) "
            "-[r:SAME_AS|NOT_SAME_AS]-"
            "(b:__Entity__ {graph_id: $graph_id_b, organisation_id: $organisation_id}) "
            "RETURN a.id AS id_a, b.id AS id_b",
            graph_id_a=graph_id_a,
            graph_id_b=graph_id_b,
            organisation_id=organisation_id,
            database_=self._database,
        )
        pairs: list[tuple[str, str]] = []
        for r in records:
            lo, hi = sorted((r["id_a"], r["id_b"]))
            pairs.append((lo, hi))
        return pairs

    def pending_cross_graph_candidates(
        self, *, organisation_id: str, graph_id: str, limit: int
    ) -> list[dict]:
        """The pending CROSS-GRAPH SAME_AS_CANDIDATE pairs touching this org-owned graph — the HITL
        review queue a reviewer reads after a generation run (the queue is otherwise only returned
        in the generation response). Matches the cross-graph candidate edges (`cross_graph = true`)
        with one endpoint in `graph_id`; org-scoped on both endpoints. Each row carries both node
        ids + both graph ids + score/method/name — the same shape the response candidates use.
        Deterministic order (score desc, then the stable pair identity); LIMIT bounds the read."""
        records, _, _ = self._driver.execute_query(
            "MATCH (a:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id}) "
            "-[c:SAME_AS_CANDIDATE]-(b:__Entity__ {organisation_id: $organisation_id}) "
            "WHERE c.cross_graph = true "
            "RETURN a.id AS id_a, a.graph_id AS graph_id_a, b.id AS id_b, "
            "b.graph_id AS graph_id_b, "
            "coalesce(a.canonical_name, a.name) AS name_a, "
            "coalesce(b.canonical_name, b.name) AS name_b, "
            "head([l IN labels(a) WHERE NOT l STARTS WITH '__']) AS label, "
            "coalesce(c.score, 0.0) AS score, c.method AS method "
            "ORDER BY score DESC, id_a, id_b LIMIT $limit",
            graph_id=graph_id,
            organisation_id=organisation_id,
            limit=limit,
            database_=self._database,
        )
        return [
            {
                "id_a": r["id_a"],
                "graph_id_a": r["graph_id_a"],
                "id_b": r["id_b"],
                "graph_id_b": r["graph_id_b"],
                "name_a": r["name_a"],
                "name_b": r["name_b"],
                "label": r["label"] or "Entity",
                "score": r["score"],
                "method": r["method"] or "unknown",
            }
            for r in records
        ]

    def candidate_endpoints_pair(
        self,
        *,
        organisation_id: str,
        graph_id_a: str,
        node_id_a: str,
        graph_id_b: str,
        node_id_b: str,
    ) -> dict | None:
        """The cross-graph twin of `candidate_endpoints`: resolve a live SAME_AS_CANDIDATE pair
        whose endpoints live in two different org-owned graphs. None if no pending candidate."""
        records, _, _ = self._driver.execute_query(
            "MATCH (a:__Entity__ {graph_id: $graph_id_a, organisation_id: $organisation_id, "
            "id: $node_id_a}) "
            "MATCH (b:__Entity__ {graph_id: $graph_id_b, organisation_id: $organisation_id, "
            "id: $node_id_b}) "
            "MATCH (a)-[:SAME_AS_CANDIDATE]-(b) "
            "RETURN a.id AS id_a, b.id AS id_b, "
            "coalesce(a.canonical_name, a.name) AS name_a, "
            "coalesce(b.canonical_name, b.name) AS name_b",
            graph_id_a=graph_id_a,
            graph_id_b=graph_id_b,
            organisation_id=organisation_id,
            node_id_a=node_id_a,
            node_id_b=node_id_b,
            database_=self._database,
        )
        if not records:
            return None
        r = records[0]
        return {"id_a": r["id_a"], "id_b": r["id_b"], "name_a": r["name_a"], "name_b": r["name_b"]}

    def link_candidate(
        self,
        *,
        organisation_id: str,
        graph_id_a: str,
        node_id_a: str,
        graph_id_b: str,
        node_id_b: str,
    ) -> bool:
        """Approve a CROSS-GRAPH candidate: MERGE a SAME_AS link (both graph ids stamped) and
        delete the candidate edge. A link, never a fold — nodes stay in their own graphs.
        Idempotent: a replay re-MERGEs the same SAME_AS and finds no candidate edge."""
        records, _, _ = self._driver.execute_query(
            "MATCH (a:__Entity__ {graph_id: $graph_id_a, organisation_id: $organisation_id, "
            "id: $node_id_a}) "
            "MATCH (b:__Entity__ {graph_id: $graph_id_b, organisation_id: $organisation_id, "
            "id: $node_id_b}) "
            "OPTIONAL MATCH (a)-[c:SAME_AS_CANDIDATE]-(b) "
            "WITH a, b, c, coalesce(c.score, 1.0) AS confidence "
            "MERGE (a)-[s:SAME_AS]->(b) "
            "SET s.organisation_id = $organisation_id, "
            "s.graph_id_a = $graph_id_a, s.graph_id_b = $graph_id_b, "
            "s.confidence = confidence, s.cross_graph = true, "
            "s.detected_by = 'cross_graph_resolution' "
            "DELETE c "
            "RETURN true AS linked",
            graph_id_a=graph_id_a,
            graph_id_b=graph_id_b,
            organisation_id=organisation_id,
            node_id_a=node_id_a,
            node_id_b=node_id_b,
            database_=self._database,
        )
        return bool(records and records[0]["linked"])

    def suppress_candidate_pair(
        self,
        *,
        organisation_id: str,
        graph_id_a: str,
        node_id_a: str,
        graph_id_b: str,
        node_id_b: str,
    ) -> bool:
        """Reject a CROSS-GRAPH candidate: MERGE a NOT_SAME_AS suppression (both graph ids
        stamped, so re-generation skips the pair) and drop the candidate edge. Idempotent."""
        records, _, _ = self._driver.execute_query(
            "MATCH (a:__Entity__ {graph_id: $graph_id_a, organisation_id: $organisation_id, "
            "id: $node_id_a}) "
            "MATCH (b:__Entity__ {graph_id: $graph_id_b, organisation_id: $organisation_id, "
            "id: $node_id_b}) "
            "MERGE (a)-[s:NOT_SAME_AS]-(b) "
            "SET s.organisation_id = $organisation_id, "
            "s.graph_id_a = $graph_id_a, s.graph_id_b = $graph_id_b, s.cross_graph = true "
            "WITH a, b "
            "OPTIONAL MATCH (a)-[c:SAME_AS_CANDIDATE]-(b) "
            "DELETE c "
            "RETURN true AS suppressed",
            graph_id_a=graph_id_a,
            graph_id_b=graph_id_b,
            organisation_id=organisation_id,
            node_id_a=node_id_a,
            node_id_b=node_id_b,
            database_=self._database,
        )
        return bool(records and records[0]["suppressed"])

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
