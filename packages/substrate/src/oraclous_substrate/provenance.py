"""Substrate provenance-collector seam (Layer 1).

The single write path for substrate provenance/audit events. Every substrate
state change emits one structured event through ``ProvenanceCollector.emit``;
callers never write provenance directly to a store (CLAUDE.md §3.7; Threat
Catalogue T7-M1). Fail-closed: an event missing any required field is rejected
before anything is written.

Audit retention and payload capture are out of R0.5 scope (later releases).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """A structured provenance event carrying the T7-M1 required fields."""

    organisation_id: str
    principal: str
    action: str
    resource: str
    outcome: str


class ProvenanceSink(Protocol):
    """Persists provenance records. The collector's only collaborator."""

    async def write(self, record: ProvenanceRecord) -> None: ...


class ProvenanceCollector:
    """The single, validated emit path for provenance events."""

    def __init__(self, sink: ProvenanceSink) -> None:
        # Private on purpose: the only way to write provenance is emit(), which
        # enforces the required-field contract — no direct-to-store bypass.
        self._sink = sink

    async def emit(self, record: ProvenanceRecord) -> None:
        for f in fields(record):
            if not str(getattr(record, f.name)).strip():
                raise ValueError(f"provenance record missing required field: {f.name}")
        await self._sink.write(record)
