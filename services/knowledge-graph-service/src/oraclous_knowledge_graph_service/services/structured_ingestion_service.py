"""Structured (CSV/JSON) ingestion use-case (ORAA-4 §21 services layer).

The recipe-driven path: decompose the source (primitive) -> pick a recipe (a supplied/stored recipe,
else a synthesised default) -> run the engine over the org-scoped writer. Synchronous (the engine +
sync Neo4j driver), so the Celery worker calls it via `asyncio.to_thread`. Org id is resolved by the
caller (the worker, from the bound context) and passed in explicitly — no contextvar-in-thread.
"""

from __future__ import annotations

from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.repositories.recipe_write_repository import RecipeGraphWriter
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.structured.default_recipe import build_default_recipe
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
    def __init__(self, *, driver, organisation_id: str, database: str | None = None) -> None:
        self._driver = driver
        self._org = organisation_id
        self._db = database
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
    ) -> dict:
        family = "csv" if (source_type or "").lower() in {"csv", "tsv"} else "json"
        primitive = self._primitives[family]
        representation = primitive.decompose(text, ExtractionMode.FULL, name=document)
        record_units = [u for u in representation.units if u.kind.value == "record"]
        if not record_units:
            raise StructuredIngestionError("no records found in the structured source")
        active_recipe = recipe or build_default_recipe(representation)
        writer = RecipeGraphWriter(
            self._driver, graph_id=graph_id, organisation_id=self._org, database=self._db
        )
        result = self._engine.execute(active_recipe, representation, writer)
        return result.as_dict()
