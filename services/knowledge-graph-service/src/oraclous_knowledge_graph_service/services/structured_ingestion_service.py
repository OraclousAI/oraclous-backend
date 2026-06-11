"""Structured (CSV/JSON) ingestion use-case (ORAA-4 §21 services layer).

The recipe-driven path: decompose the source (primitive) -> pick a recipe (a supplied/stored recipe,
else a synthesised default) -> run the engine over the org-scoped writer. Synchronous (the engine +
sync Neo4j driver), so the Celery worker calls it via `asyncio.to_thread`. Org id is resolved by the
caller (the worker, from the bound context) and passed in explicitly — no contextvar-in-thread.
"""

from __future__ import annotations

from datetime import UTC, datetime

from oraclous_knowledge_graph_service.core.config import Settings, get_settings
from oraclous_knowledge_graph_service.domain.ontology import Ontology
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.repositories.recipe_write_repository import RecipeGraphWriter
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.recipes.extraction_pass import run_extraction_pass
from oraclous_knowledge_graph_service.services.recipes.similarity_pass import run_similarity_pass
from oraclous_knowledge_graph_service.services.structured.default_recipe import build_default_recipe
from oraclous_knowledge_graph_service.services.structured.extractors import StructuredParseError
from oraclous_knowledge_graph_service.services.structured.primitives import (
    CsvPrimitive,
    JsonPrimitive,
)

_STRUCTURED_TYPES = {"csv", "tsv", "json", "jsonl"}


def is_structured(source_type: str) -> bool:
    return (source_type or "").lower() in _STRUCTURED_TYPES


class StructuredIngestionError(Exception):
    """Structured ingestion failed (parse error or empty source)."""


class StructuredIngestionService:
    def __init__(
        self,
        *,
        driver,
        organisation_id: str,
        database: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._driver = driver
        self._org = organisation_id
        self._db = database
        self._settings = settings or get_settings()
        self._engine = get_recipe_engine()
        self._primitives = {"csv": CsvPrimitive(), "json": JsonPrimitive()}

    def ingest(
        self,
        *,
        graph_id: str,
        document: str,
        text: str,
        source_type: str,
        recipe: dict | None = None,
        ontology: Ontology | None = None,
        temporal: dict | None = None,
    ) -> dict:
        family = "csv" if (source_type or "").lower() in {"csv", "tsv"} else "json"
        primitive = self._primitives[family]
        try:
            representation = primitive.decompose(text, ExtractionMode.FULL, name=document)
        except StructuredParseError as exc:
            # A JSONL source with an un-parseable tail must fail loudly (records would be dropped),
            # not silently ingest a partial graph (ORAA-263).
            raise StructuredIngestionError(str(exc)) from exc
        record_units = [u for u in representation.units if u.kind.value == "record"]
        if not record_units:
            raise StructuredIngestionError("no records found in the structured source")
        active_recipe = recipe or build_default_recipe(representation)
        writer = RecipeGraphWriter(
            self._driver, graph_id=graph_id, organisation_id=self._org, database=self._db
        )
        result = self._engine.execute(
            active_recipe, representation, writer, ontology=ontology, temporal=temporal
        )
        # Hybrid free-text-on-a-field (Slice 2): AFTER the deterministic projection, mine entities
        # from each record's prose field and MERGE MENTIONS edges from the record's primary node.
        # Reuses the SAME org-scoped writer (so the entities are stamped + deterministic-id MERGEd
        # exactly like the projected nodes). Fail-soft: no-extractor / per-record error is skipped.
        if active_recipe.get("extractions") or active_recipe.get("similarities"):
            meta = {
                "recipe_id": active_recipe["id"],
                "recipe_version": active_recipe["version"],
                "ingestion_time": datetime.now(UTC).isoformat(),
            }
            if active_recipe.get("extractions"):
                ex_stats = run_extraction_pass(
                    recipe=active_recipe,
                    representation=representation,
                    writer=writer,
                    node_index_by_rule=result.node_index_by_rule,
                    settings=self._settings,
                    engine=self._engine,
                    meta=meta,
                    source_id=result.source_id,
                )
                result.entities_extracted = ex_stats.entities_extracted
                result.mentions = ex_stats.mentions
                result.warnings.extend(ex_stats.warnings)
            # Slice 3 — content similarity: AFTER the extraction pass, embed each record's `from`
            # field + cosine kNN, MERGE-ing SIMILAR_TO edges between similar records. Reuses the
            # SAME writer + settings; fail-soft on an embed() error (the pass is skipped).
            if active_recipe.get("similarities"):
                sim_stats = run_similarity_pass(
                    recipe=active_recipe,
                    representation=representation,
                    writer=writer,
                    node_index_by_rule=result.node_index_by_rule,
                    settings=self._settings,
                    engine=self._engine,
                    meta=meta,
                    source_id=result.source_id,
                )
                result.similarity_edges = sim_stats.similarity_edges
                result.warnings.extend(sim_stats.warnings)
        return result.as_dict()
