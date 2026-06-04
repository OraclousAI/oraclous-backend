"""Health DTO (ORAA-4 §21 schema layer — Pydantic only)."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
