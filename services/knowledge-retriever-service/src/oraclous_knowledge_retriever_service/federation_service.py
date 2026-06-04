"""Federation service — cross-graph federated query and SAME_AS deduplication (ORAA-59)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_GRAPH_IDS: int = 10
MAX_TOTAL_RESULTS: int = 200


class FederationError(Exception):
    def __init__(self, message: str = "", status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class FederatedQueryOptions:
    deduplicate_entities: bool = False
    include_cross_graph_links: bool = False
    max_results: int = MAX_TOTAL_RESULTS


# Alias used by integration tests
FederationQueryOptions = FederatedQueryOptions


@dataclass
class CrossGraphLink:
    link_type: str
    graph_a: str
    graph_b: str


@dataclass
class FederatedEntity:
    entity_id: str
    name: str
    type: str
    source_graph_id: str


@dataclass
class SameAsCandidate:
    entity_a_id: str
    entity_b_id: str
    graph_a: str
    graph_b: str
    score: float
    signals: dict[str, Any] = field(default_factory=dict)


class FederationService:
    def __init__(self, driver: Any, neo4j_database: str | None = None) -> None:
        self._driver = driver
        self._database = neo4j_database

    def _session_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self._database is not None:
            kwargs["database"] = self._database
        return kwargs

    async def _validate_graphs(self, user_id: str, graph_ids: list[str]) -> None:
        async with self._driver.session(**self._session_kwargs()) as session:
            result = await session.run(
                "MATCH (g:Graph:__Rebac__) WHERE g.graph_id IN $graph_ids "
                "RETURN g.graph_id AS graph_id, g.owner_user_id AS user_id, "
                "g.name AS name, g.federatable AS federatable",
                {"graph_ids": list(graph_ids)},
            )
            rows = await result.data()

        found_ids = {row["graph_id"] for row in rows}
        for gid in graph_ids:
            if gid not in found_ids:
                raise FederationError(f"Graph {gid!r} not found", status_code=400)

        for row in rows:
            if row["user_id"] != user_id:
                raise FederationError(
                    f"Graph {row['graph_id']!r} is not owned by the requesting user",
                    status_code=403,
                )
            if not row["federatable"]:
                raise FederationError(
                    f"Graph {row['graph_id']!r} is not federatable",
                    status_code=400,
                )

    async def federated_query(
        self,
        user_id: str,
        graph_ids: list[str],
        query: str,
        options: FederatedQueryOptions | None = None,
    ) -> dict[str, Any]:
        if len(graph_ids) > MAX_GRAPH_IDS:
            raise FederationError(
                f"Too many graph_ids: {len(graph_ids)} > {MAX_GRAPH_IDS}",
                status_code=400,
            )
        opts = options or FederatedQueryOptions()

        await self._validate_graphs(user_id, graph_ids)

        async with self._driver.session(**self._session_kwargs()) as session:
            result = await session.run(
                "UNWIND $graph_ids AS gid "
                "MATCH (e:__Entity__ {graph_id: gid}) "
                "WHERE toLower(e.name) CONTAINS toLower($query) "
                "RETURN e.id AS entity_id, e.name AS name, e.type AS type, "
                "gid AS source_graph_id",
                {"graph_ids": list(graph_ids), "query": query},
            )
            entity_rows = await result.data()

        entities = [
            FederatedEntity(
                entity_id=row.get("entity_id", ""),
                name=row.get("name", ""),
                type=row.get("type", ""),
                source_graph_id=row.get("source_graph_id", ""),
            )
            for row in entity_rows
        ]

        cross_graph_links: list[CrossGraphLink] = []
        if opts.deduplicate_entities and opts.include_cross_graph_links:
            groups: dict[tuple[str, str], list[FederatedEntity]] = {}
            for entity in entities:
                key = (entity.name, entity.type)
                groups.setdefault(key, []).append(entity)
            for group in groups.values():
                distinct_graphs = sorted({e.source_graph_id for e in group})
                if len(distinct_graphs) >= 2:
                    for i in range(len(distinct_graphs) - 1):
                        cross_graph_links.append(
                            CrossGraphLink(
                                link_type="SAME_AS",
                                graph_a=distinct_graphs[i],
                                graph_b=distinct_graphs[i + 1],
                            )
                        )

        return {
            "status": "ok",
            "graphs_queried": list(graph_ids),
            "entities": entities,
            "cross_graph_links": cross_graph_links,
        }

    async def federated_vector_search(
        self,
        user_id: str,
        graph_ids: list[str],
        query_text: str,
        options: FederatedQueryOptions | None = None,
    ) -> dict[str, Any]:
        if len(graph_ids) > MAX_GRAPH_IDS:
            raise FederationError(
                f"Too many graph_ids: {len(graph_ids)} > {MAX_GRAPH_IDS}",
                status_code=400,
            )
        await self._validate_graphs(user_id, graph_ids)
        return {
            "status": "ok",
            "graphs_queried": list(graph_ids),
            "entities": [],
        }

    async def find_same_as_candidates(
        self,
        user_id: str,
        source_graph_id: str,
        target_graph_ids: list[str],
        *,
        threshold: float = 0.8,
    ) -> list[SameAsCandidate]:
        await self._validate_graphs(user_id, [source_graph_id] + list(target_graph_ids))
        async with self._driver.session(**self._session_kwargs()) as session:
            result = await session.run(
                "MATCH (ea:__Entity__ {graph_id: $source}) "
                "MATCH (eb:__Entity__) WHERE eb.graph_id IN $targets "
                "AND ea.name = eb.name AND ea.type = eb.type "
                "RETURN ea.id AS a_id, eb.id AS b_id, "
                "$source AS graph_a, eb.graph_id AS graph_b, 1.0 AS score",
                {"source": source_graph_id, "targets": list(target_graph_ids)},
            )
            rows = await result.data()
        return [
            SameAsCandidate(
                entity_a_id=row["a_id"],
                entity_b_id=row["b_id"],
                graph_a=row["graph_a"],
                graph_b=row["graph_b"],
                score=row["score"],
                signals={
                    "name": 1.0,
                    "type": 1.0,
                    "embedding": 0.0,
                    "shared_relations": 0.0,
                },
            )
            for row in rows
        ]

    async def resolve_entity(
        self,
        user_id: str,
        entity_id: str,
        graph_id: str,
        *,
        include_same_as: bool = False,
    ) -> dict[str, Any]:
        await self._validate_graphs(user_id, [graph_id])
        async with self._driver.session(**self._session_kwargs()) as session:
            result = await session.run(
                "MATCH (e:__Entity__ {id: $entity_id, graph_id: $graph_id}) "
                "RETURN e.id AS entity_id, e.name AS name, e.type AS type, "
                "$graph_id AS source_graph_id",
                {"entity_id": entity_id, "graph_id": graph_id},
            )
            row = await result.single()
        if row is None:
            return {}
        if hasattr(row, "data"):
            return dict(row.data())
        return {
            "entity_id": row["entity_id"],
            "name": row["name"],
            "type": row["type"],
            "source_graph_id": row["source_graph_id"],
        }

    async def find_federation_candidates(
        self,
        user_id: str,
        graph_id: str,
        target_graph_ids: list[str],
        *,
        min_score: float = 0.5,
    ) -> list[dict[str, Any]]:
        await self._validate_graphs(user_id, [graph_id] + list(target_graph_ids))
        async with self._driver.session(**self._session_kwargs()) as session:
            result = await session.run(
                "MATCH (ea:__Entity__ {graph_id: $source}) "
                "MATCH (eb:__Entity__) WHERE eb.graph_id IN $targets "
                "AND ea.name = eb.name AND ea.type = eb.type "
                "RETURN ea.id AS a_id, eb.id AS b_id, "
                "ea.name AS name, ea.type AS type, "
                "$source AS source_graph_id, eb.graph_id AS target_graph_id",
                {"source": graph_id, "targets": list(target_graph_ids)},
            )
            rows = await result.data()
        return [
            {
                "entity_a_id": row["a_id"],
                "entity_b_id": row["b_id"],
                "score": 1.0,
                "signals": {
                    "name": 1.0,
                    "type": 1.0,
                    "embedding": 0.0,
                    "shared_relations": 0.0,
                },
            }
            for row in rows
        ]
