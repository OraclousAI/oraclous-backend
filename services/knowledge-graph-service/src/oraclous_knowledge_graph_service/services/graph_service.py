"""Graph use-cases (services layer — all business logic lives here, not in routes).

Replaces the legacy inline `GraphNodeService` stub that lived *inside* the route module
(a service-architecture violation). The org scope is enforced fail-closed in the repository
(`enforced_organisation_id`); this layer adds TWO gates on top (#736 / ADR-051) — an org-scoped
read gate and a per-user owner gate for writes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid

from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)

logger = logging.getLogger(__name__)

# Display names reserved for system-owned graphs (#332/ADR-027 §5): a user cannot create a graph
# named like the agent-memory default graph, so the two surfaces never collide on name. The
# definitive marker is the `system_kind` column (the name guard is the friendly front door).
_RESERVED_GRAPH_NAMES: frozenset[str] = frozenset({"agent memory"})


class GraphNotFound(Exception):
    """Raised when a graph is not visible to the caller (wrong org or not owned). Maps to 404."""


class ReservedGraphName(Exception):
    """The requested graph name is reserved for a system-owned graph (#332/ADR-027 §5). Maps to
    409 — a user-created graph may never take a reserved name (so it can never shadow the lazily
    created agent-memory default graph)."""


class GraphService:
    """Graph CRUD use-cases. TWO gates on top of org-scoping (#736 / ADR-051): reads pass the
    org-scoped read gate, writes keep the per-user owner gate."""

    def __init__(
        self, repo: GraphRepository, write_repo: GraphWriteRepository | None = None
    ) -> None:
        self._repo = repo
        self._write_repo = write_repo

    async def _with_live_counts(self, graph: Graph) -> Graph:
        """Overlay the LIVE Neo4j node/relationship counts onto the graph (org+graph scoped).

        The `node_count`/`relationship_count` Postgres columns are stale (ingestion writes real
        nodes to Neo4j, never back to Postgres), so when a Neo4j-backed write repo is wired we
        replace them with the live counts. When it is not (unit tests / unconfigured substrate),
        the graph is returned unchanged — the Postgres-column fallback.
        """
        if self._write_repo is None:
            return graph
        try:
            node_count, relationship_count = await asyncio.to_thread(
                self._write_repo.count_for_graph,
                graph_id=str(graph.id),
                organisation_id=str(graph.organisation_id),
            )
        except Exception as exc:  # noqa: BLE001 — degrade-don't-crash: a Neo4j hiccup must not
            # turn a Postgres-backed metadata read into a 500; fall back to the stored columns.
            logger.warning(
                "live Neo4j count failed for graph %s; using stored counts: %s", graph.id, exc
            )
            return graph
        return dataclasses.replace(
            graph, node_count=node_count, relationship_count=relationship_count
        )

    async def create_graph(
        self, *, user_id: uuid.UUID, name: str, description: str | None
    ) -> Graph:
        if name.strip().lower() in _RESERVED_GRAPH_NAMES:
            raise ReservedGraphName(name)
        # system_kind is left NULL — a user route can never mint a system-owned graph.
        return await self._repo.create(user_id=user_id, name=name, description=description)

    async def list_graphs(self) -> list[Graph]:
        """The workspaces the caller can see: ALL graphs in their organisation (#736 / ADR-051).

        Deliberately the SAME set as `list_org_graphs` — it delegates to it rather than repeating
        the query, so the console list and the ADR-026 federation fan-out can never drift apart.
        The rows carry their owner's `user_id`, which is what lets the response mark "mine" without
        a second query; ownership is reported here, not required.

        This was `list_for_user`, and its narrowness protected nothing: every graph in the set is
        already readable by every org member through the org-scoped `POST /v1/search/*`.
        """
        return [await self._with_live_counts(g) for g in await self.list_org_graphs()]

    async def list_org_graphs(self) -> list[Graph]:
        """The federation accessible-set (#330 / ADR-026): ALL graphs in the caller's bound org.

        Deliberately org-scoped, NOT owner-gated — it mirrors exactly the gate the retriever's
        single-graph reads apply (org-scope only), so federation aggregates precisely the graphs
        the caller can already read individually and never one more. No live-count overlay: the
        consumer (KRS) needs only ids + names for fan-out and labeling.
        """
        return await self._repo.list_for_org()

    # ── the two gates (#736 / ADR-051) ────────────────────────────────────────────────────────
    #
    # Read and manage are separate decisions, so they are separate checks. `_owned_or_404` is the
    # WRITE gate and is unchanged; `_readable_or_404` is the READ gate. Relaxing the one gate in
    # place would have widened ingest, the ontology write and resolution decisions along with the
    # reads — ADR-051 decision 3 draws the line exactly here.

    async def _owned_or_404(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> Graph:
        """WRITE gate (no live-count overlay) — the raw stored graph or a 404-mapped error.

        Behind every mutation: create, rename, delete, ingest submit, the ontology write, community
        detection, summarisation, resolution decisions and the cross-org grant."""
        graph = await self._repo.get(graph_id)
        if graph is None or graph.user_id != user_id:
            raise GraphNotFound(str(graph_id))
        return graph

    async def _readable_or_404(self, *, graph_id: uuid.UUID) -> Graph:
        """READ gate — org-scoped, no owner filter (ADR-051 decision 2).

        The organisation comes from the bound governance context inside the repository, never from
        the request, so this is the org-scoped floor ADR-018 sets and ADR-026 §1 composes — the same
        gate the retriever's single-graph reads already apply. A graph in another organisation is
        not visible at all, so it raises `GraphNotFound` exactly as before."""
        graph = await self._repo.get(graph_id)
        if graph is None:
            raise GraphNotFound(str(graph_id))
        return graph

    async def assert_readable(self, *, graph_id: uuid.UUID) -> None:
        """Gate-only form of the read gate, for the read paths that need permission but not the
        graph (documents, artifacts, the ontology read, analytics) — so none of them pays for the
        live-count overlay it would throw away."""
        await self._readable_or_404(graph_id=graph_id)

    async def get_readable_graph(self, *, graph_id: uuid.UUID) -> Graph:
        """One graph for READING, org-scoped, with live counts. The detail route's gate."""
        return await self._with_live_counts(await self._readable_or_404(graph_id=graph_id))

    async def assert_owned(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Public owner gate for cross-cutting callers (e.g. the cross-org grant): raises
        ``GraphNotFound`` (→ 404, no leak) unless the graph is owned by ``user_id`` in the bound
        org. Only a graph's owner may share it."""
        await self._owned_or_404(graph_id=graph_id, user_id=user_id)

    async def get_graph(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> Graph:
        """OWNER-gated fetch with live counts — the gate the write paths hold. Reads use
        `get_readable_graph` / `assert_readable` instead (#736)."""
        graph = await self._owned_or_404(graph_id=graph_id, user_id=user_id)
        return await self._with_live_counts(graph)

    async def update_graph(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str | None,
        description: str | None,
        model_credential_id: str | None = None,
    ) -> Graph:
        # owner gate first (a graph owned by another user in the same org -> 404, no leak)
        await self._owned_or_404(graph_id=graph_id, user_id=user_id)
        updated = await self._repo.update(
            graph_id,
            name=name,
            description=description,
            model_credential_id=model_credential_id,
        )
        if updated is None:
            raise GraphNotFound(str(graph_id))
        return updated

    async def delete_graph(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> None:
        # Owner gate first (a graph in another org/owner -> 404, no leak). Org-scoping is enforced
        # by `_owned_or_404`; the Neo4j cascade below is graph_id-scoped to the owned graph.
        await self._owned_or_404(graph_id=graph_id, user_id=user_id)
        # Cascade the graph's Neo4j nodes/edges before the Postgres row, so a Neo4j failure aborts
        # the delete (surfaced, not swallowed) and the metadata row survives to retry — never an
        # orphaned graph (the leak this fixes). When the substrate is unwired (unit tests /
        # Neo4j unconfigured) `write_repo` is None and only the Postgres row is removed.
        if self._write_repo is not None:
            await asyncio.to_thread(self._write_repo.delete_graph_nodes, graph_id=str(graph_id))
        await self._repo.delete(graph_id)


# Legacy module-level name preserved (existing tests patch `GraphNodeService`).
GraphNodeService = GraphService
