"""Re-embed repository (repositories layer — the only Neo4j driver access for the re-embed pass).

#949 Q2. When a workspace moves from the keyless hashing embedder to a real one, its already-stored
chunks are left in the old vector space. The read side refuses to compare across spaces rather than
returning a meaningless cosine, so without this pass the workspace would simply stop answering
meaning-based searches until someone re-ingested every document by hand.

Two operations, both bound to ONE (organisation, graph) scope for the life of the object, with both
predicates bound as parameters on every statement (§3.3; Neo4j Community has no RLS, so isolation
is the query's own job and the org value comes from the fail-closed governance seam):

* :meth:`list_chunks_needing_reembed` — a bounded page of chunks NOT in the target space.
* :meth:`write_embedding` — rewrite one chunk's vector and stamp the new identity.

The stale scan is the whole of the pass's restartability. It only ever selects chunks that do not
already match, so a crashed run resumes by re-reading, a completed workspace scans to empty, and a
graph part-way through is simply a graph with fewer left — no cursor, no resume token, no state to
get out of sync with the store.
"""

from __future__ import annotations

from typing import Any

from neo4j import Driver
from oraclous_embedding import LEGACY_NULL_EMBEDDER_ID

# A chunk carrying no `embedder_id` predates the identity stamp and lives in the legacy hashing
# space, so it is stale for any other target — the same reading the read-side filter applies, from
# the same shared constant, because the two disagreeing is exactly the #643 failure.
_IS_CURRENT = (
    "(c.embedder_id = $embedder_id "
    "OR (c.embedder_id IS NULL AND $embedder_id = $legacy_embedder_id))"
)


class ReembedRepository:
    def __init__(
        self,
        driver: Driver,
        *,
        organisation_id: str,
        graph_id: str,
        database: str | None = None,
    ) -> None:
        self._driver = driver
        self._org = organisation_id
        self._graph = graph_id
        self._db = database

    def _query(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            cypher,
            organisation_id=self._org,
            graph_id=self._graph,
            database_=self._db,
            **params,
        )
        return [r.data() for r in records]

    def list_chunks_needing_reembed(self, *, embedder_id: str, limit: int) -> list[dict]:
        """A bounded page of this scope's chunks whose vectors are NOT in the target space.

        Ordered by chunk id so a page boundary is reproducible rather than storage-order luck.
        `limit` is always the caller's batch size: a workspace's chunk count is unbounded, and
        reading all of it into one worker's memory is the failure mode every other sweep in this
        service already avoids.
        """
        return self._query(
            "MATCH (c:Chunk) "
            "WHERE c.graph_id = $graph_id AND c.organisation_id = $organisation_id "
            "AND c.text IS NOT NULL "
            f"AND NOT {_IS_CURRENT} "
            "RETURN c.id AS id, c.text AS text "
            "ORDER BY c.id LIMIT $limit",
            embedder_id=embedder_id,
            legacy_embedder_id=LEGACY_NULL_EMBEDDER_ID,
            limit=limit,
        )

    def write_embedding(
        self, *, chunk_id: str, embedding: list[float], embedder_id: str, embedding_dim: int
    ) -> None:
        """Rewrite one chunk's vector and stamp the identity that produced it.

        A SET on the matched node — never a create and never a re-key. The chunk's text has not
        changed, so its id must not change either, or every relationship and citation pointing at
        it would break on the pass's first run. The identity is written in the SAME statement as
        the vector, so a crash can never leave a new vector labelled with the old identity (which
        would silently exclude a correctly re-embedded chunk from search, or worse, include a stale
        one).
        """
        self._query(
            "MATCH (c:Chunk) "
            "WHERE c.graph_id = $graph_id AND c.organisation_id = $organisation_id "
            "AND c.id = $chunk_id "
            "SET c.embedding = $embedding, c.embedder_id = $embedder_id, "
            "c.embedding_dim = $embedding_dim "
            "RETURN c.id AS id",
            chunk_id=chunk_id,
            embedding=embedding,
            embedder_id=embedder_id,
            embedding_dim=embedding_dim,
        )


def enumerate_graphs_needing_reembed(
    driver: Driver, *, embedder_id: str, database: str | None = None, limit: int | None = None
) -> list[tuple[str, str]]:
    """Every (organisation_id, graph_id) still holding a chunk outside the target space.

    The beat dispatcher's input, mirroring `enumerate_memory_graphs`. Bounded by `limit` so one
    sweep cannot enqueue an unbounded fan-out; a graph missed by the cap is picked up by the next
    sweep, because the predicate is a fact about the store rather than a queue position.
    """
    cypher = (
        "MATCH (c:Chunk) "
        "WHERE c.organisation_id IS NOT NULL AND c.graph_id IS NOT NULL "
        "AND c.text IS NOT NULL "
        f"AND NOT {_IS_CURRENT} "
        "RETURN DISTINCT c.organisation_id AS org, c.graph_id AS graph "
        "ORDER BY org, graph"
    )
    params: dict[str, Any] = {
        "embedder_id": embedder_id,
        "legacy_embedder_id": LEGACY_NULL_EMBEDDER_ID,
    }
    if limit is not None:
        cypher += " LIMIT $limit"
        params["limit"] = limit
    records, _, _ = driver.execute_query(cypher, database_=database, **params)
    return [(str(r["org"]), str(r["graph"])) for r in records]
