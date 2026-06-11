"""Org-scoped recipe-graph writer (ORAA-4 §21 repositories layer — the only Neo4j driver access).

Holds the unified-graph-model Cypher lifted from legacy `develop@84152635 recipes/engine.py`,
reshaped so `organisation_id` is threaded into EVERY MERGE/MATCH key map and stamped on create (next
to the legacy `graph_id`). The recipe engine (services) plans and calls these methods; it never
touches a driver. Labels / relationship types / property keys are f-string-interpolated into Cypher
only after passing `_safe` (the validate-then-interpolate contract — defense in depth on top of the
engine's own check). All writes are idempotent MERGEs.
"""

from __future__ import annotations

import re

from neo4j import Driver

_BATCH = 500
_SAFE_IDENTIFIER = re.compile(r"^(?!__)[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_LABELS = frozenset(
    {"__Platform__", "__Entity__", "__KGBuilder__", "__Rebac__", "__System__"}
)


class UnsafeIdentifierError(ValueError):
    """An identifier reached the Cypher-interpolation boundary without passing the allowlist."""


def _safe(identifier: str) -> str:
    if not isinstance(identifier, str) or not _SAFE_IDENTIFIER.match(identifier):
        raise UnsafeIdentifierError(f"unsafe identifier at write boundary: {identifier!r}")
    if identifier in _RESERVED_LABELS:
        raise UnsafeIdentifierError(f"reserved identifier at write boundary: {identifier!r}")
    return identifier


def _chunks(items: list, size: int = _BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


_SOURCE_CYPHER = """
MERGE (s:Source:__KGBuilder__ {graph_id: $graph_id, organisation_id: $organisation_id,
                               source_id: $source_id})
ON CREATE SET s.source_type = $source_type, s.shape_signature = $shape_signature,
              s.ingestion_source = $source_id, s.provenance = 'EXTRACTED',
              s.recipe_id = $recipe_id, s.recipe_version = $recipe_version,
              s.ingestion_time = $ingestion_time
"""

_LINK_CONTAINERS_CYPHER = """
UNWIND $batch AS row
MATCH (child:__KGBuilder__ {graph_id: $graph_id, organisation_id: $organisation_id, id: row.child})
MATCH (parent:__KGBuilder__
       {graph_id: $graph_id, organisation_id: $organisation_id, id: row.parent})
MERGE (child)-[:PART_OF {graph_id: $graph_id, organisation_id: $organisation_id}]->(parent)
"""

_DERIVED_FROM_CONTAINER_CYPHER = """
MATCH (e:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, id: $entity_id})
MATCH (c:__KGBuilder__ {graph_id: $graph_id, organisation_id: $organisation_id, id: $container_id})
MERGE (e)-[:DERIVED_FROM {graph_id: $graph_id, organisation_id: $organisation_id}]->(c)
"""

_DERIVED_FROM_SOURCE_CYPHER = """
MATCH (e:__Entity__ {graph_id: $graph_id, organisation_id: $organisation_id, id: $entity_id})
MATCH (s:Source {graph_id: $graph_id, organisation_id: $organisation_id, source_id: $source_id})
MERGE (e)-[:DERIVED_FROM {graph_id: $graph_id, organisation_id: $organisation_id}]->(s)
"""


class RecipeGraphWriter:
    """Executes the recipe engine's writes, org+graph scoped (fail-closed on the resolved org)."""

    def __init__(
        self, driver: Driver, *, graph_id: str, organisation_id: str, database: str | None = None
    ) -> None:
        self._driver = driver
        self.graph_id = graph_id
        self._org = organisation_id
        self._db = database

    def _run(self, cypher: str, params: dict) -> None:
        self._driver.execute_query(
            cypher, organisation_id=self._org, graph_id=self.graph_id, database_=self._db, **params
        )

    def write_source(
        self, *, source_id: str, source_type: str, shape_signature: str, meta: dict
    ) -> None:
        self._run(
            _SOURCE_CYPHER,
            {
                "source_id": source_id,
                "source_type": source_type,
                "shape_signature": shape_signature,
                "recipe_id": meta["recipe_id"],
                "recipe_version": meta["recipe_version"],
                "ingestion_time": meta["ingestion_time"],
            },
        )

    def write_containers(self, *, label: str, rows: list[dict], source_id: str, meta: dict) -> None:
        label = _safe(label)
        cypher = (
            "UNWIND $batch AS row\n"
            f"MERGE (c:{label}:__KGBuilder__ "
            "{graph_id: $graph_id, organisation_id: $organisation_id, id: row.id})\n"
            "ON CREATE SET c.unit_id = row.unit_id, c.name = row.name,\n"
            "              c.ingestion_source = $source_id, c.provenance = 'EXTRACTED',\n"
            "              c.recipe_id = $recipe_id, c.recipe_version = $recipe_version,\n"
            "              c.ingestion_time = $ingestion_time\n"
            "WITH c, row\n"
            "MATCH (s:Source "
            "{graph_id: $graph_id, organisation_id: $organisation_id, source_id: $source_id})\n"
            "MERGE (c)-[:PART_OF {graph_id: $graph_id, organisation_id: $organisation_id}]->(s)"
        )
        for batch in _chunks(rows):
            self._run(
                cypher,
                {
                    "batch": batch,
                    "source_id": source_id,
                    "recipe_id": meta["recipe_id"],
                    "recipe_version": meta["recipe_version"],
                    "ingestion_time": meta["ingestion_time"],
                },
            )

    def link_containers(self, *, pairs: list[dict]) -> None:
        """pairs: [{child: <id>, parent: <id>}] — nested container PART_OF."""
        for batch in _chunks(pairs):
            self._run(_LINK_CONTAINERS_CYPHER, {"batch": batch})

    def merge_node(
        self,
        *,
        label: str,
        entity_id: str,
        identity_key: str,
        properties: dict,
        provenance: str,
        source_id: str,
        meta: dict,
        confidence: float | None,
        container_id: str | None,
    ) -> None:
        label = _safe(label)
        node_cypher = (
            "UNWIND $batch AS row\n"
            f"MERGE (e:{label}:__Entity__ "
            "{graph_id: $graph_id, organisation_id: $organisation_id, id: row.id})\n"
            "ON CREATE SET e.ingestion_source = $source_id, e.provenance = $provenance,\n"
            "              e.recipe_id = $recipe_id, e.recipe_version = $recipe_version,\n"
            "              e.ingestion_time = $ingestion_time\n"
            # A node first MERGEd as a foreign_key stub (merge_edge_to_stub) carries `stub = true`
            # and no identity_key; the real target-record ingest reaches the SAME id and enriches
            # it — stamp the identity_key + clear the stub flag so the node is no longer a stub.
            "SET e.identity_key = row.identity_key, e.stub = false\n"
            "SET e += row.properties"
        )
        self._run(
            node_cypher,
            {
                "batch": [
                    {"id": entity_id, "identity_key": identity_key, "properties": properties}
                ],
                "source_id": source_id,
                "provenance": provenance,
                "recipe_id": meta["recipe_id"],
                "recipe_version": meta["recipe_version"],
                "ingestion_time": meta["ingestion_time"],
            },
        )
        if provenance == "INFERRED" and confidence is not None:
            confidence_cypher = (
                f"MATCH (e:{label}:__Entity__ "
                "{graph_id: $graph_id, organisation_id: $organisation_id, id: $id})\n"
                "SET e.confidence = coalesce(e.confidence, $confidence)"
            )
            self._run(confidence_cypher, {"id": entity_id, "confidence": confidence})
        if container_id is not None:
            self._run(
                _DERIVED_FROM_CONTAINER_CYPHER,
                {"entity_id": entity_id, "container_id": container_id},
            )
        else:
            self._run(_DERIVED_FROM_SOURCE_CYPHER, {"entity_id": entity_id, "source_id": source_id})

    def set_property(self, *, prop_name: str, targets: list[dict]) -> int:
        prop_name = _safe(prop_name)
        cypher = (
            "UNWIND $batch AS row\n"
            "MATCH (e:__Entity__ "
            "{graph_id: $graph_id, organisation_id: $organisation_id, id: row.id})\n"
            f"SET e.{prop_name} = row.value"
        )
        written = 0
        for batch in _chunks(targets):
            self._run(cypher, {"batch": batch})
            written += len(batch)
        return written

    def merge_edge(
        self, *, rel_type: str, edges: list[dict], source_id: str, provenance: str, meta: dict
    ) -> int:
        rel_type = _safe(rel_type)
        cypher = (
            "UNWIND $batch AS row\n"
            "MATCH (a:__Entity__ "
            "{graph_id: $graph_id, organisation_id: $organisation_id, id: row.from})\n"
            "MATCH (b:__Entity__ "
            "{graph_id: $graph_id, organisation_id: $organisation_id, id: row.to})\n"
            f"MERGE (a)-[r:{rel_type} "
            "{graph_id: $graph_id, organisation_id: $organisation_id}]->(b)\n"
            "ON CREATE SET r.ingestion_source = $source_id, r.provenance = $provenance,\n"
            "              r.recipe_id = $recipe_id, r.recipe_version = $recipe_version,\n"
            "              r.ingestion_time = $ingestion_time"
        )
        written = 0
        for batch in _chunks(edges):
            self._run(
                cypher,
                {
                    "batch": batch,
                    "source_id": source_id,
                    "provenance": provenance,
                    "recipe_id": meta["recipe_id"],
                    "recipe_version": meta["recipe_version"],
                    "ingestion_time": meta["ingestion_time"],
                },
            )
            written += len(batch)
        return written

    def merge_edge_to_stub(
        self,
        *,
        rel_type: str,
        target_label: str,
        edges: list[dict],
        source_id: str,
        provenance: str,
        meta: dict,
    ) -> int:
        """Recipe `foreign_key` edges (G1): the source node exists, but the target is addressed
        only by its deterministic id (the FK value resolved through the target rule's identity). The
        target may not be ingested yet — possibly in a separate run/file — so MERGE the target node
        by id as a stub on first sight (the later target-record ingest enriches the SAME id via
        :merge_node), then MERGE the edge. Each edge row carries the resolved target_identity_key so
        a stub records what value it stands for.
        """
        rel_type = _safe(rel_type)
        target_label = _safe(target_label)
        cypher = (
            "UNWIND $batch AS row\n"
            "MATCH (a:__Entity__ "
            "{graph_id: $graph_id, organisation_id: $organisation_id, id: row.from})\n"
            f"MERGE (b:{target_label}:__Entity__ "
            "{graph_id: $graph_id, organisation_id: $organisation_id, id: row.to})\n"
            "ON CREATE SET b.ingestion_source = $source_id, b.provenance = $provenance,\n"
            "              b.recipe_id = $recipe_id, b.recipe_version = $recipe_version,\n"
            "              b.ingestion_time = $ingestion_time,\n"
            "              b.identity_key = row.target_identity_key, b.stub = true\n"
            f"MERGE (a)-[r:{rel_type} "
            "{graph_id: $graph_id, organisation_id: $organisation_id}]->(b)\n"
            "ON CREATE SET r.ingestion_source = $source_id, r.provenance = $provenance,\n"
            "              r.recipe_id = $recipe_id, r.recipe_version = $recipe_version,\n"
            "              r.ingestion_time = $ingestion_time"
        )
        written = 0
        for batch in _chunks(edges):
            self._run(
                cypher,
                {
                    "batch": batch,
                    "source_id": source_id,
                    "provenance": provenance,
                    "recipe_id": meta["recipe_id"],
                    "recipe_version": meta["recipe_version"],
                    "ingestion_time": meta["ingestion_time"],
                },
            )
            written += len(batch)
        return written
