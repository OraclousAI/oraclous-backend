"""Ontology DTOs (ORAA-4 §21 schema layer — Pydantic only).

Slice B: the request/response carry an optional TYPED layer (entity/relationship type definitions +
free-text extraction hints) on top of the legacy labels-only shape. Every field is optional, so an
existing labels-only client keeps working unchanged; a typed client additionally drives free-text
hard-schema extraction. `allowed_labels` is derived from `entity_types` when those are supplied.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EntityTypeDef(BaseModel):
    name: str
    description: str | None = None
    properties: list[str] = Field(default_factory=list)


class RelationshipTypeDef(BaseModel):
    name: str
    source: str | None = None
    target: str | None = None


class OntologyRequest(BaseModel):
    allowed_labels: list[str] = Field(default_factory=list)
    mode: Literal["open", "strict", "coerce"]
    # Typed layer (optional) — drives free-text hard-schema extraction.
    entity_types: list[EntityTypeDef] = Field(default_factory=list)
    relationship_types: list[RelationshipTypeDef] = Field(default_factory=list)
    # Free-text extraction hints (optional, soft steering).
    domain: str | None = None
    density: Literal["sparse", "balanced", "dense"] | None = None
    focus: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)


class OntologyResponse(BaseModel):
    allowed_labels: list[str]
    mode: str
    entity_types: list[EntityTypeDef] = Field(default_factory=list)
    relationship_types: list[RelationshipTypeDef] = Field(default_factory=list)
    domain: str | None = None
    density: str | None = None
    focus: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)


class SuggestOntologyRequest(BaseModel):
    """Schema synthesis (authoring aid, Slice C): infer a typed ontology from a text sample."""

    sample: str = Field(..., min_length=1, description="A text sample to infer an ontology from.")
    mode: Literal["open", "strict", "coerce"] = "strict"


class SuggestedOntologyResponse(BaseModel):
    """The inferred ontology in the Slice-B shape — saveable verbatim via the ontology PUT."""

    mode: str
    entity_types: list[EntityTypeDef] = Field(default_factory=list)
    relationship_types: list[RelationshipTypeDef] = Field(default_factory=list)
