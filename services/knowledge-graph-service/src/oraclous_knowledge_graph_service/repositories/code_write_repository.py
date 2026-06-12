"""Org-scoped code-graph writer (ORAA-4 §21 repositories layer — the only Neo4j driver access).

Reshaped from legacy `develop@84152635 code_parser_service.write_code_graph_sync` (Stage 5) +
the delta/embedding/stale-sweep stages (1/4/6, #305): ordered idempotent MERGEs, with
`organisation_id` threaded into every key map (next to `graph_id`). Identity: :File on
(org, graph, path); symbols on (org, graph, qualified_name); :Dependency on (org, graph, name).
Replace-per-file (delete the file's existing symbols before re-writing) makes re-ingest idempotent
AND prunes symbols removed from a changed file. Labels are FIXED (no user input) so there is no
injection surface here.

The org+graph scope is server-injected at construction — the caller can never override it, and
every Cypher carries `organisation_id` so no read/write ever crosses a tenant boundary.
"""

from __future__ import annotations

from neo4j import Driver

_BATCH = 500
# Symbol labels carrying a `stale_at` lifecycle (Stage 1 marks them, Stage 6 sweeps them).
_SYMBOL_LABELS = ("Function", "Class", "Variable")


def _chunks(items: list, size: int = _BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class CodeGraphWriteRepository:
    def __init__(
        self, driver: Driver, *, graph_id: str, organisation_id: str, database: str | None = None
    ) -> None:
        self._driver = driver
        self._graph = graph_id
        self._org = organisation_id
        self._db = database

    def _run(self, cypher: str, **params) -> None:
        self._driver.execute_query(
            cypher,
            organisation_id=self._org,
            graph_id=self._graph,
            database_=self._db,
            **params,
        )

    def existing_file_hashes(self, paths: list[str]) -> dict[str, str]:
        """Stage 1 read: return ``{path: content_hash}`` for the :File nodes already in this graph.

        Org+graph scoped — the delta is computed only against this tenant's files."""
        out: dict[str, str] = {}
        for batch in _chunks(paths):
            result = self._driver.execute_query(
                """
                UNWIND $paths AS p
                MATCH (f:File {graph_id: $graph_id, organisation_id: $organisation_id, path: p})
                RETURN f.path AS path, f.content_hash AS hash
                """,
                organisation_id=self._org,
                graph_id=self._graph,
                paths=batch,
                database_=self._db,
            )
            for rec in result.records:
                out[rec["path"]] = rec["hash"]
        return out

    def mark_symbols_stale(self, paths: list[str]) -> None:
        """Stage 1: stamp ``stale_at = datetime()`` on the existing symbols of changed files.

        Marks (does not delete) so a re-appearing symbol can be revived (the write clears
        ``stale_at``); a symbol removed from the file stays stale and is swept by Stage 6."""
        for batch in _chunks(paths):
            self._run(
                """
                UNWIND $paths AS p
                MATCH (f:File {graph_id: $graph_id, organisation_id: $organisation_id, path: p})
                OPTIONAL MATCH (f)<-[:DEFINED_IN]-(sym)
                WHERE sym:Function OR sym:Class OR sym:Variable
                SET sym.stale_at = datetime()
                """,
                paths=batch,
            )

    def replace_files(self, files: list[dict]) -> None:
        """MERGE each :File (upsert). Symbol pruning is handled by the Stage 1 stale-mark + Stage 6
        sweep (a changed file's symbols are marked stale, then revived by the re-write or swept), so
        this no longer hard-deletes — that would defeat the delta lifecycle."""
        for batch in _chunks(files):
            self._run(
                """
                UNWIND $batch AS f
                MERGE (file:File:__KGBuilder__
                       {graph_id: $graph_id, organisation_id: $organisation_id, path: f.path})
                SET file.language = f.language, file.content_hash = f.content_hash,
                    file.size_bytes = f.size_bytes, file.ingestion_source = 'code'
                """,
                batch=batch,
            )

    def write_dependencies(self, deps: list[dict]) -> None:
        """Stage 0 write: MERGE :Dependency nodes (org+graph scoped, FIXED label)."""
        for batch in _chunks(deps):
            self._run(
                """
                UNWIND $batch AS d
                MERGE (dep:Dependency:__KGBuilder__
                       {graph_id: $graph_id, organisation_id: $organisation_id, name: d.name})
                SET dep.version_constraint = d.version_constraint, dep.dep_type = d.dep_type
                """,
                batch=batch,
            )

    def write_symbols(self, symbols: list[dict]) -> None:
        """Create symbol nodes (FIXED label) + DEFINED_IN -> file + METHOD_OF -> class."""
        for label in ("Class", "Function", "Variable"):
            rows = [s for s in symbols if s["label"] == label]
            for batch in _chunks(rows):
                self._run(
                    f"""
                    UNWIND $batch AS s
                    MATCH (file:File
                           {{graph_id: $graph_id, organisation_id: $organisation_id,
                             path: s.file_path}})
                    MERGE (n:{label}:__Entity__:__KGBuilder__
                           {{graph_id: $graph_id, organisation_id: $organisation_id,
                             qualified_name: s.qualified_name}})
                    SET n += s.properties, n.stale_at = null
                    MERGE (n)-[:DEFINED_IN
                          {{graph_id: $graph_id, organisation_id: $organisation_id}}]->(file)
                    """,
                    batch=batch,
                )
        method_pairs = [
            {"fn": s["qualified_name"], "cls": s["properties"]["parent_class"]}
            for s in symbols
            if s["label"] == "Function" and s["properties"].get("parent_class")
        ]
        for batch in _chunks(method_pairs):
            self._run(
                """
                UNWIND $batch AS m
                MATCH (fn:Function {graph_id: $graph_id, organisation_id: $organisation_id,
                                    qualified_name: m.fn})
                MATCH (cls:Class {graph_id: $graph_id, organisation_id: $organisation_id,
                                  qualified_name: m.cls})
                MERGE (fn)-[:METHOD_OF
                      {graph_id: $graph_id, organisation_id: $organisation_id}]->(cls)
                """,
                batch=batch,
            )

    def write_edges(self, *, calls: list[dict], inherits: list[dict], imports: list[dict]) -> None:
        for batch in _chunks(calls):
            self._run(
                """
                UNWIND $batch AS c
                MATCH (a:Function {graph_id: $graph_id, organisation_id: $organisation_id,
                                   qualified_name: c.caller})
                MATCH (b:Function {graph_id: $graph_id, organisation_id: $organisation_id,
                                   qualified_name: c.callee})
                MERGE (a)-[:CALLS {graph_id: $graph_id, organisation_id: $organisation_id}]->(b)
                """,
                batch=batch,
            )
        for batch in _chunks(inherits):
            self._run(
                """
                UNWIND $batch AS i
                MATCH (a:Class {graph_id: $graph_id, organisation_id: $organisation_id,
                                qualified_name: i.child})
                MATCH (b:Class {graph_id: $graph_id, organisation_id: $organisation_id,
                                qualified_name: i.parent})
                MERGE (a)-[:INHERITS {graph_id: $graph_id, organisation_id: $organisation_id}]->(b)
                """,
                batch=batch,
            )
        # IMPORTS: internal -> :CodeModule, external -> :Dependency (org+graph scoped target nodes)
        internal = [i for i in imports if i["is_internal"]]
        external = [i for i in imports if not i["is_internal"]]
        for rows, target_label in ((internal, "CodeModule"), (external, "Dependency")):
            for batch in _chunks(rows):
                self._run(
                    f"""
                    UNWIND $batch AS im
                    MATCH (file:File {{graph_id: $graph_id, organisation_id: $organisation_id,
                                       path: im.source_file}})
                    MERGE (t:{target_label}:__KGBuilder__
                           {{graph_id: $graph_id, organisation_id: $organisation_id,
                             name: im.target}})
                    MERGE (file)-[:IMPORTS
                          {{graph_id: $graph_id, organisation_id: $organisation_id}}]->(t)
                    """,
                    batch=batch,
                )

    def write_embeddings(self, embeddings: list[dict]) -> None:
        """Stage 4 write: set the `embedding` vector on each Function/Class by qualified_name.

        Each row is ``{"label": "Function"|"Class", "qualified_name": str, "embedding": [float]}``.
        Org+graph scoped; a node absent in this tenant simply matches nothing (no cross-tenant
        write)."""
        for label in ("Function", "Class"):
            rows = [e for e in embeddings if e["label"] == label]
            for batch in _chunks(rows):
                self._run(
                    f"""
                    UNWIND $batch AS e
                    MATCH (n:{label} {{graph_id: $graph_id, organisation_id: $organisation_id,
                                       qualified_name: e.qualified_name}})
                    SET n.embedding = e.embedding
                    """,
                    batch=batch,
                )

    # NOTE — Stage 4 vector index: a label-wide Neo4j vector index over :Function/:Class CANNOT be
    # org-scoped (Neo4j 5.x vector indexes key only on the single vector property — no composite,
    # no filter property), so a kNN over it would return cross-org neighbours with no RLS backstop
    # (ADR-006, the ORG005 guard). The durable, org-scoped Stage-4 artifact is the `embedding`
    # property itself (written by `write_embeddings`, org+graph scoped). The accelerating vector
    # INDEX + the over-fetch-then-org-filter read belong to the knowledge-retriever code-search
    # slice (the only layer that can apply the org filter at read time) — tracked under #294, not
    # built here. See the PR/issue report for this deferral.

    def delete_stale_symbols(self, *, ttl_days: int) -> int:
        """Stage 6 sweep: detach-delete code symbols marked `stale_at` older than the TTL.

        Org+graph scoped; returns the count deleted. Runs in batches of `_BATCH` (a bounded MATCH +
        DETACH DELETE per call) so one sweep never holds a single huge lock — looping until a batch
        deletes nothing."""
        total = 0
        while True:
            result = self._driver.execute_query(
                """
                MATCH (n)
                WHERE (n:Function OR n:Class OR n:Variable)
                  AND n.graph_id = $graph_id AND n.organisation_id = $organisation_id
                  AND n.stale_at IS NOT NULL
                  AND n.stale_at < datetime() - duration({days: $ttl_days})
                WITH n LIMIT $batch
                DETACH DELETE n
                RETURN count(n) AS deleted
                """,
                organisation_id=self._org,
                graph_id=self._graph,
                ttl_days=ttl_days,
                batch=_BATCH,
                database_=self._db,
            )
            rec = result.records[0] if result.records else None
            deleted = int(rec["deleted"]) if rec else 0
            total += deleted
            if deleted < _BATCH:
                break
        return total
