"""Org-scoped read repository (ORAA-4 §21 repositories layer — the only Neo4j driver access).

Every read is scoped by organisation_id (from the bound governance context, passed in by the route)
AND graph_id, both as bound parameters — never interpolated (Community has no RLS, so isolation is
enforced in-query + the org value comes from the fail-closed seam). Sync driver calls; the service
runs them off the event loop. Semantic similarity is brute-force cosine computed in Cypher via
`reduce` (both the stored chunk embedding and the query vector are L2-normalised, so dot = cosine) —
no vector index, no API key, works on Community.
"""

from __future__ import annotations

from neo4j import Driver

_COSINE = "reduce(s = 0.0, i IN range(0, size(c.embedding) - 1) | s + c.embedding[i] * $qvec[i])"


class RetrievalRepository:
    def __init__(
        self, driver: Driver, *, organisation_id: str, database: str | None = None
    ) -> None:
        self._driver = driver
        self._org = organisation_id
        self._db = database

    def _query(self, cypher: str, **params) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            cypher, organisation_id=self._org, database_=self._db, **params
        )
        return [r.data() for r in records]

    def semantic(self, *, graph_id: str, qvec: list[float], top_k: int) -> list[dict]:
        return self._query(
            "MATCH (c:Chunk) "
            "WHERE c.graph_id = $graph_id AND c.organisation_id = $organisation_id "
            "AND c.embedding IS NOT NULL "
            f"WITH c, {_COSINE} AS score "
            "RETURN elementId(c) AS id, labels(c) AS labels, properties(c) AS props, score "
            "ORDER BY score DESC LIMIT $top_k",
            graph_id=graph_id,
            qvec=qvec,
            top_k=top_k,
        )

    def fulltext(self, *, graph_id: str, query: str, top_k: int) -> list[dict]:
        # Index-free, read-only lexical match (ORAA-58 / T6: KRS issues no write Cypher, so it never
        # creates a fulltext index). Case-insensitive substring over :Chunk text, org+graph scoped.
        return self._query(
            "MATCH (c:Chunk) "
            "WHERE c.graph_id = $graph_id AND c.organisation_id = $organisation_id "
            "AND c.text IS NOT NULL AND toLower(c.text) CONTAINS toLower($query) "
            "RETURN elementId(c) AS id, labels(c) AS labels, properties(c) AS props, 1.0 AS score "
            "LIMIT $top_k",
            graph_id=graph_id,
            query=query,
            top_k=top_k,
        )

    def neighbors(self, *, graph_id: str, node_id: str, top_k: int) -> list[dict]:
        return self._query(
            "MATCH (n) WHERE elementId(n) = $node_id AND n.graph_id = $graph_id "
            "AND n.organisation_id = $organisation_id "
            "MATCH (n)-[r]-(m) "
            "WHERE m.graph_id = $graph_id AND m.organisation_id = $organisation_id "
            "RETURN elementId(m) AS id, labels(m) AS labels, properties(m) AS props, "
            "type(r) AS relationship, 1.0 AS score LIMIT $top_k",
            graph_id=graph_id,
            node_id=node_id,
            top_k=top_k,
        )

    def similar(self, *, graph_id: str, node_id: str, top_k: int, min_score: float) -> list[dict]:
        # find_similar (#310): the SIMILAR_TO neighbours of a node, ranked by the edge's `score`
        # (the cosine the KGS similarity pass stamped). Undirected match — SIMILAR_TO is stored once
        # per pair in a canonical direction, so a node can be on either end. Org+graph scoped on the
        # anchor, the neighbour, AND (defence-in-depth) the score gate is applied in-query. An edge
        # with no score sorts last (coalesced to 0) but still returns above min_score=0 — callers
        # default min_score to 0 to see every SIMILAR_TO link.
        return self._query(
            "MATCH (n) WHERE elementId(n) = $node_id AND n.graph_id = $graph_id "
            "AND n.organisation_id = $organisation_id "
            "MATCH (n)-[r:SIMILAR_TO]-(m) "
            "WHERE m.graph_id = $graph_id AND m.organisation_id = $organisation_id "
            "AND coalesce(r.score, 0.0) >= $min_score "
            "WITH m, r, coalesce(r.score, 0.0) AS sim ORDER BY sim DESC LIMIT $top_k "
            "RETURN elementId(m) AS id, labels(m) AS labels, properties(m) AS props, "
            "type(r) AS relationship, sim AS score",
            graph_id=graph_id,
            node_id=node_id,
            top_k=top_k,
            min_score=min_score,
        )

    def subgraph(self, *, graph_id: str, limit: int) -> dict:
        # A bounded slice for visualisation: take up to `limit` nodes (org+graph scoped), then the
        # edges whose BOTH endpoints fall inside that set. Edges come from a directed pattern
        # comprehension per node (no APOC); collect() always yields one row (even for an empty
        # graph), so the FE gets {nodes, edges} without an empty-result special case. Each edge
        # carries its full property bag (mirrors the node `props`), so edge-level scores written by
        # the resolver (e.g. `score` on SIMILAR_TO/SAME_AS_CANDIDATE) reach the FE explorer.
        rows = self._query(
            "MATCH (n) WHERE n.graph_id = $graph_id AND n.organisation_id = $organisation_id "
            "WITH n LIMIT $limit "
            "WITH collect(n) AS ns "
            "RETURN [a IN ns | {id: elementId(a), labels: labels(a), props: properties(a)}] "
            "AS nodes, [a IN ns | [(a)-[r]->(b) WHERE b IN ns | "
            "{source: elementId(a), target: elementId(b), type: type(r), "
            "properties: properties(r)}]] AS edge_groups",
            graph_id=graph_id,
            limit=limit,
        )
        if not rows:
            return {"nodes": [], "edges": []}
        row = rows[0]
        edges = [edge for group in row.get("edge_groups", []) for edge in group]
        return {"nodes": row.get("nodes", []), "edges": edges}

    def graph_exists(self, *, graph_id: str) -> bool:
        # Org-scoped existence probe for the evaluate endpoint (#331): another org's graph is
        # indistinguishable from a nonexistent one (the caller 404s), so graph ids never leak
        # across organisations. Anchored to :Chunk like every other KRS read (#333) — an
        # unlabelled MATCH on a miss scans the whole node store; the label keeps the probe a
        # probe. A graph with no chunks has nothing to evaluate anyway. LIMIT 1.
        rows = self._query(
            "MATCH (c:Chunk) WHERE c.graph_id = $graph_id AND c.organisation_id = $organisation_id "
            "RETURN elementId(c) AS id LIMIT 1",
            graph_id=graph_id,
        )
        return bool(rows)

    def temporal(self, *, graph_id: str, as_of: str, top_k: int) -> list[dict]:
        return self._query(
            "MATCH (n) WHERE n.graph_id = $graph_id AND n.organisation_id = $organisation_id "
            "AND n.valid_from IS NOT NULL AND n.valid_from <= $as_of "
            "AND (n.valid_to IS NULL OR n.valid_to >= $as_of) "
            "RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props, 1.0 AS score "
            "ORDER BY n.valid_from DESC LIMIT $top_k",
            graph_id=graph_id,
            as_of=as_of,
            top_k=top_k,
        )
