"""Ontology use-cases (ORAA-4 §21 services layer) — set/get a graph's label ontology.

Owner-gated (reuses GraphService's gate). Validates the mode and that every allowed label is a safe
identifier (the same allowlist the recipe writer enforces) before persisting, so a stored ontology
can never inject an unsafe label into Cypher at projection time.
"""

from __future__ import annotations

import re
import uuid

from oraclous_knowledge_graph_service.domain.ontology import MODES
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.services.graph_service import GraphService

_SAFE_LABEL = re.compile(r"^(?!__)[A-Za-z_][A-Za-z0-9_]*$")


class OntologyError(Exception):
    """Invalid ontology (bad mode or unsafe label). Maps to 422."""


class OntologyService:
    def __init__(self, graph_repo: GraphRepository, graph_service: GraphService) -> None:
        self._graphs_repo = graph_repo
        self._graphs = graph_service

    async def get(self, *, user_id: uuid.UUID, graph_id: uuid.UUID) -> dict:
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)  # owner gate -> 404
        ontology = await self._graphs_repo.get_ontology(graph_id)
        return ontology or {"allowed_labels": [], "mode": "open"}

    async def set(
        self, *, user_id: uuid.UUID, graph_id: uuid.UUID, allowed_labels: list[str], mode: str
    ) -> dict:
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)
        if mode not in MODES:
            raise OntologyError(f"mode must be one of {MODES}")
        for label in allowed_labels:
            if not _SAFE_LABEL.match(label):
                raise OntologyError(f"unsafe label {label!r}")
        if mode in ("strict", "coerce") and not allowed_labels:
            raise OntologyError(f"mode {mode!r} requires at least one allowed label")
        ontology = {"allowed_labels": allowed_labels, "mode": mode}
        await self._graphs_repo.set_ontology(graph_id, ontology)
        return ontology
