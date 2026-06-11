"""Ontology use-cases (ORAA-4 §21 services layer) — set/get a graph's label ontology.

Owner-gated (reuses GraphService's gate). Validates the mode and that every allowed label is a safe
identifier (the same allowlist the recipe writer enforces) before persisting, so a stored ontology
can never inject an unsafe label into Cypher at projection time.

Slice B: the ontology can additionally carry TYPED entity/relationship definitions + free-text
extraction hints. Entity/relationship/property names are validated with the SAME safe-identifier
rule; `allowed_labels` is derived from `entity_types` when those are supplied (the typed defs are
authoritative). The stored shape is the domain `Ontology.as_dict` round-trip — back-compatible with
existing labels-only rows, which parse and re-serialise unchanged.
"""

from __future__ import annotations

import re
import uuid

from oraclous_knowledge_graph_service.domain.ontology import (
    MODES,
    EntityType,
    Ontology,
    RelType,
)
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.services.graph_service import GraphService

_SAFE_LABEL = re.compile(r"^(?!__)[A-Za-z_][A-Za-z0-9_]*$")


class OntologyError(Exception):
    """Invalid ontology (bad mode or unsafe label). Maps to 422."""


def _require_safe(name: str, *, kind: str) -> None:
    if not _SAFE_LABEL.match(name):
        raise OntologyError(f"unsafe {kind} {name!r}")


class OntologyService:
    def __init__(self, graph_repo: GraphRepository, graph_service: GraphService) -> None:
        self._graphs_repo = graph_repo
        self._graphs = graph_service

    async def get(self, *, user_id: uuid.UUID, graph_id: uuid.UUID) -> dict:
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)  # owner gate -> 404
        ontology = await self._graphs_repo.get_ontology(graph_id)
        if not ontology:
            return {"allowed_labels": [], "mode": "open"}
        # Round-trip through the domain so a legacy labels-only row is normalised and a typed row
        # carries its derived allowed_labels + typed defs back to the client.
        parsed = Ontology.of(ontology)
        return parsed.as_dict() if parsed else {"allowed_labels": [], "mode": "open"}

    async def set(
        self,
        *,
        user_id: uuid.UUID,
        graph_id: uuid.UUID,
        allowed_labels: list[str],
        mode: str,
        entity_types: list[dict] | None = None,
        relationship_types: list[dict] | None = None,
        domain: str | None = None,
        density: str | None = None,
        focus: list[str] | None = None,
        ignore: list[str] | None = None,
    ) -> dict:
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)
        if mode not in MODES:
            raise OntologyError(f"mode must be one of {MODES}")

        entity_types = entity_types or []
        relationship_types = relationship_types or []

        # Validate every identifier that can reach Cypher at projection time.
        for label in allowed_labels:
            _require_safe(label, kind="label")
        ets: list[EntityType] = []
        for e in entity_types:
            _require_safe(e["name"], kind="entity type")
            for prop in e.get("properties") or []:
                _require_safe(prop, kind="property")
            ets.append(EntityType.of(e))
        rts: list[RelType] = []
        declared = {e.name for e in ets}
        for r in relationship_types:
            _require_safe(r["name"], kind="relationship type")
            for endpoint in (r.get("source"), r.get("target")):
                if endpoint is not None:
                    _require_safe(endpoint, kind="relationship endpoint")
                    if endpoint not in declared:
                        raise OntologyError(
                            f"relationship {r['name']!r} references undefined entity type "
                            f"{endpoint!r}"
                        )
            rts.append(RelType.of(r))

        # When typed entity defs are present, allowed_labels is derived from them.
        effective_labels = tuple(e.name for e in ets) if ets else tuple(allowed_labels)
        if mode in ("strict", "coerce") and not effective_labels:
            raise OntologyError(f"mode {mode!r} requires at least one allowed label")

        ontology = Ontology(
            allowed_labels=effective_labels,
            mode=mode,
            entity_types=tuple(ets),
            relationship_types=tuple(rts),
            domain=domain or None,
            density=density or None,
            focus=tuple(focus or ()),
            ignore=tuple(ignore or ()),
        )
        stored = ontology.as_dict()
        await self._graphs_repo.set_ontology(graph_id, stored)
        return stored
