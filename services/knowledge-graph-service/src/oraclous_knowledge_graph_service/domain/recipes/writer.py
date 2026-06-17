"""Recipe-graph writer port (ORAA-4 §21 domain layer — a structural contract, no I/O).

The recipe engine (``services/recipes``) and its extraction/similarity passes plan the unified-graph
writes and call them on an injected *writer*. The live writer is the org-scoped
``repositories.recipe_write_repository.RecipeGraphWriter`` (the only Neo4j driver access); the
authoring dry-run path injects an in-memory recording double instead. Both must satisfy the SAME
surface so the engine type-checks against either without probing for methods at runtime
(``getattr``).

This ``RecipeGraphWriter`` Protocol captures that surface exactly. It is the type the engine + the
passes annotate against; the concrete repository writer and the dry-run double each satisfy it
structurally (no explicit subclassing). Keeping the contract in the domain layer means the engine
depends on the *shape*, not the repository class — and the dry-run double is a first-class
implementation rather than a structural-but-incomplete match.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RecipeGraphWriter(Protocol):
    """The unified-graph write surface the recipe engine + passes call (org+graph scoped writes)."""

    graph_id: str

    def write_source(
        self, *, source_id: str, source_type: str, shape_signature: str, meta: dict
    ) -> None: ...

    def write_containers(
        self, *, label: str, rows: list[dict], source_id: str, meta: dict
    ) -> None: ...

    def link_containers(self, *, pairs: list[dict]) -> None: ...

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
        aliases: list[str] | None = ...,
    ) -> None: ...

    def set_property(self, *, prop_name: str, targets: list[dict]) -> int: ...

    def merge_edge(
        self, *, rel_type: str, edges: list[dict], source_id: str, provenance: str, meta: dict
    ) -> int: ...

    def merge_candidate_edges(
        self, *, edges: list[dict], source_id: str, provenance: str, meta: dict
    ) -> int: ...

    def merge_edge_to_stub(
        self,
        *,
        rel_type: str,
        target_label: str,
        edges: list[dict],
        source_id: str,
        provenance: str,
        meta: dict,
    ) -> int: ...
