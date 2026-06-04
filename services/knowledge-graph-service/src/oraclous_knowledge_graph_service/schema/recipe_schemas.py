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
