"""Recipe dry-run use-case (ORAA-4 §21 services layer) — an authoring aid (Slice C).

Preview what a STRUCTURED (CSV/JSON) recipe + sample would project into the graph WITHOUT touching
Neo4j: decompose the sample (the same primitives the real path uses), pick the recipe (supplied,
else the synthesised default) and run the SAME deterministic recipe engine — but against a recording
no-op writer instead of ``RecipeGraphWriter``. The writer captures the planned node labels, edge
types and counts; it opens no driver and issues no Cypher, so a dry-run is side-effect-free.

The optional ``ontology`` is passed through so the preview reports ontology violations (a strict
ontology that rejects a recipe's label shows up as ``ontology_violations`` and a skipped node),
matching exactly what the real ingest would do.

Free-text (unstructured) recipes are NOT previewed deterministically — their projection needs the
LLM entity extractor — so a non-structured ``source_type`` is reported as ``requires_llm`` rather
than guessed at.
"""

from __future__ import annotations

from typing import Any

from oraclous_knowledge_graph_service.domain.ontology import Ontology
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.structured.default_recipe import build_default_recipe
from oraclous_knowledge_graph_service.services.structured.extractors import StructuredParseError
from oraclous_knowledge_graph_service.services.structured.primitives import (
    CsvPrimitive,
    JsonPrimitive,
)
from oraclous_knowledge_graph_service.services.structured_ingestion_service import is_structured

_DRY_RUN_GRAPH_ID = "dry-run"


class DryRunError(Exception):
    """The sample could not be parsed / contained no records. Maps to 422."""


class _RecordingNoOpWriter:
    """A ``RecipeGraphWriter``-shaped collaborator that records intent and writes NOTHING.

    It opens no Neo4j driver and runs no Cypher — every method appends to an in-memory tally, so the
    engine plans exactly as it would for a live ingest while the graph stays untouched. The methods
    return the same counts the real writer returns so the engine's totals are accurate.
    """

    def __init__(self, graph_id: str) -> None:
        self.graph_id = graph_id
        self.node_labels: list[str] = []
        self.edge_types: list[str] = []
        self.container_labels: list[str] = []
        self.source_count = 0

    def write_source(self, *, source_id, source_type, shape_signature, meta) -> None:
        self.source_count += 1

    def write_containers(self, *, label, rows, source_id, meta) -> None:
        self.container_labels.extend([label] * len(rows))

    def link_containers(self, *, pairs) -> None:
        self._noted = len(pairs)  # recorded, never written

    def merge_node(
        self,
        *,
        label,
        entity_id,
        identity_key,
        properties,
        provenance,
        source_id,
        meta,
        confidence,
        container_id,
    ) -> None:
        self.node_labels.append(label)

    def set_property(self, *, prop_name, targets) -> int:
        return len(targets)

    def merge_edge(self, *, rel_type, edges, source_id, provenance, meta) -> int:
        self.edge_types.extend([rel_type] * len(edges))
        return len(edges)

    def merge_edge_to_stub(
        self, *, rel_type, target_label, edges, source_id, provenance, meta
    ) -> int:
        self.edge_types.extend([rel_type] * len(edges))
        return len(edges)


def _label_counts(labels: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return counts


class DryRunService:
    """Preview a recipe projection over a sample with no Neo4j writes."""

    def __init__(self) -> None:
        self._engine = get_recipe_engine()
        self._primitives = {"csv": CsvPrimitive(), "json": JsonPrimitive()}

    def preview(
        self,
        *,
        sample: str,
        source_type: str = "json",
        recipe: dict | None = None,
        ontology: dict | None = None,
    ) -> dict[str, Any]:
        """Return a structured preview of the planned projection (no writes).

        Shape: ``{source_type, recipe_id, node_labels{label:count}, relationship_types[], counts,
        ontology_violations, warnings}``. A non-structured ``source_type`` returns
        ``{requires_llm: True}``.
        """
        if not is_structured(source_type):
            return {
                "source_type": source_type,
                "requires_llm": True,
                "note": (
                    "free-text recipes are projected by the LLM entity extractor; a deterministic "
                    "dry-run preview is only available for structured (CSV/JSON) recipes."
                ),
            }

        family = "csv" if (source_type or "").lower() in {"csv", "tsv"} else "json"
        primitive = self._primitives[family]
        try:
            representation = primitive.decompose(sample, ExtractionMode.FULL)
        except StructuredParseError as exc:
            raise DryRunError(str(exc)) from exc
        record_units = [u for u in representation.units if u.kind.value == "record"]
        if not record_units:
            raise DryRunError("no records found in the sample")

        active_recipe = recipe or build_default_recipe(representation)
        parsed_ontology = Ontology.of(ontology) if ontology else None
        writer = _RecordingNoOpWriter(_DRY_RUN_GRAPH_ID)
        result = self._engine.execute(
            active_recipe, representation, writer, ontology=parsed_ontology
        )

        return {
            "source_type": source_type,
            "recipe_id": result.recipe_id,
            "node_labels": _label_counts(writer.node_labels),
            "relationship_types": sorted(set(writer.edge_types)),
            "container_labels": _label_counts(writer.container_labels),
            "counts": {
                "nodes": result.nodes_written,
                "edges": result.edges_written,
                "containers": result.containers_written,
                "properties": result.properties_written,
                "units_skipped": result.units_skipped,
            },
            "ontology_violations": result.ontology_violations,
            "warnings": result.warnings,
        }
