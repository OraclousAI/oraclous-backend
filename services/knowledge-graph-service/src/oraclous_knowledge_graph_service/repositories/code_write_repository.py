"""Org-scoped code-graph writer (ORAA-4 §21 repositories layer — the only Neo4j driver access).

Reshaped from legacy `develop@84152635 code_parser_service.write_code_graph_sync` (Stage 5): ordered
idempotent MERGEs, with `organisation_id` threaded into every key map (next to `graph_id`).
Identity: :File on (org, graph, path); symbols on (org, graph, qualified_name). Replace-per-file
(delete the file's existing symbols before re-writing) makes re-ingest idempotent AND prunes
symbols removed from a changed file. Labels are FIXED (no user input) so there is no injection
surface here. The bitemporal/embedding/stale-sweep stages are out of S4 scope.
"""

from __future__ import annotations

from neo4j import Driver

_BATCH = 500


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

    def replace_files(self, files: list[dict]) -> None:
        """MERGE each :File (upsert) and detach-delete its existing DEFINED_IN symbols."""
        for batch in _chunks(files):
            self._run(
                """
                UNWIND $batch AS f
                MERGE (file:File:__KGBuilder__
                       {graph_id: $graph_id, organisation_id: $organisation_id, path: f.path})
                SET file.language = f.language, file.content_hash = f.content_hash,
                    file.size_bytes = f.size_bytes, file.ingestion_source = 'code'
                WITH file
                OPTIONAL MATCH (file)<-[:DEFINED_IN]-(sym)
                DETACH DELETE sym
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
                    SET n += s.properties
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
