"""Structural representation vocabulary (domain layer — pure, no I/O).

Lifted verbatim from legacy `develop@84152635 knowledge-graph-builder/app/recipes/primitives/
interface.py` (ADR-022). A per-type Primitive deterministically decomposes a source into a flat list
of StructuralUnits; the recipe engine then projects them into the unified graph. Zero dependencies.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ExtractionMode(StrEnum):
    SAMPLE = "sample"
    FULL = "full"


class UnitKind(StrEnum):
    SOURCE = "source"
    TABLE = "table"
    SHEET = "sheet"
    COLUMN = "column"
    RECORD = "record"
    FIELD = "field"
    DOCUMENT = "document"
    CHUNK = "chunk"
    FILE = "file"
    SYMBOL = "symbol"


class StructuralUnit(BaseModel):
    kind: UnitKind
    unit_id: str = Field(..., description="Stable, path-like id within the source.")
    name: str | None = Field(default=None, description="A table/column/field/symbol name.")
    data_type: str | None = Field(default=None, description="Inferred type of the unit's values.")
    role: str | None = Field(
        default=None,
        description="A hint a recipe rule matches on: primary_key|foreign_key|free_text|...",
    )
    parent_id: str | None = Field(default=None, description="unit_id of the containing unit.")
    sample_values: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StructuralRepresentation(BaseModel):
    source_type: str
    shape_signature: str = Field(
        ..., description="Deterministic shape descriptor (recipe lookup key)."
    )
    mode: ExtractionMode
    units: list[StructuralUnit] = Field(default_factory=list)


@runtime_checkable
class Primitive(Protocol):
    source_type: str

    def decompose(
        self, source: Any, mode: ExtractionMode, *, name: str = ...
    ) -> StructuralRepresentation: ...
