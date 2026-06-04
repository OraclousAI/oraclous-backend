"""Ontology DTOs (ORAA-4 §21 schema layer — Pydantic only)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OntologyRequest(BaseModel):
    allowed_labels: list[str]
    mode: Literal["open", "strict", "coerce"]


class OntologyResponse(BaseModel):
    allowed_labels: list[str]
    mode: str
