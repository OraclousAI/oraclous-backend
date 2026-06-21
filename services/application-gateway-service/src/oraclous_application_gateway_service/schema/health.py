"""Health DTOs (schema layer)."""

from __future__ import annotations

from pydantic import BaseModel


class UpstreamHealth(BaseModel):
    name: str
    status: str  # "ok" | "degraded" | "down"
    latency_ms: int


class UpstreamsHealthResponse(BaseModel):
    overall: str  # "ok" | "degraded"
    upstreams: list[UpstreamHealth]
