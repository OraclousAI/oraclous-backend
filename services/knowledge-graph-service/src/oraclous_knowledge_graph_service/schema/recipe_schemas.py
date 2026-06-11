"""Recipe library DTOs (ORAA-4 §21 schema layer — Pydantic only)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StoreRecipeRequest(BaseModel):
    recipe: dict[str, Any] = Field(
        ..., description="A format-0.2 recipe document (validated server-side)."
    )


class RecipeStoredResponse(BaseModel):
    id: str
    version: int
    status: str


class RecipeSummary(BaseModel):
    id: str
    version: int
    status: str
    source_type: str
    concern: str


class DryRunRequest(BaseModel):
    """Preview a recipe over a sample (authoring aid, Slice C) — no Neo4j writes.

    Either ``recipe`` or ``ontology`` (or both) may be supplied; with neither, the default recipe is
    synthesised from the sample shape and the projection runs with no ontology constraint.
    """

    sample: str = Field(..., min_length=1, description="A small CSV/JSON sample to project.")
    source_type: str = "json"
    recipe: dict[str, Any] | None = None
    ontology: dict[str, Any] | None = None


class RecipeTemplate(BaseModel):
    """A built-in, author-ready recipe template (the evidence/conflicts pair)."""

    concern: str
    recipe: dict[str, Any]
