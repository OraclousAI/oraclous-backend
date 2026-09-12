"""Substrate provenance-collector seam (Layer 1).

The single write path for substrate provenance/audit events. Every substrate
state change emits one structured event through ``ProvenanceCollector.emit``;
callers never write provenance directly to a store (CLAUDE.md §3.7; Threat
Catalogue T7-M1). Fail-closed: an event missing any of the five required
fields is rejected before anything is written.

``ProvenanceRecord`` also carries three optional extension fields —
``context``, ``input_hash``, ``output_hash`` — so a record can attest what a
call returned, not merely that it happened. They default to ``None`` and are
never swept into the required-field check. The record never stores a raw
payload: ``hash_payload`` turns a payload into its content fingerprint
(``sha256:<hex>``) so only that fingerprint is ever attested (CLAUDE.md §11).

Audit retention is out of R0.5 scope (later releases).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

_REQUIRED_FIELDS = ("organisation_id", "principal", "action", "resource", "outcome")


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """A structured provenance event carrying the T7-M1 required fields.

    ``context``, ``input_hash``, and ``output_hash`` are the extension
    point: optional, ``None`` by default, and never required.
    """

    organisation_id: str
    principal: str
    action: str
    resource: str
    outcome: str
    context: Mapping[str, Any] | None = None
    input_hash: str | None = None
    output_hash: str | None = None


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
        for name in _REQUIRED_FIELDS:
            if not str(getattr(record, name)).strip():
                raise ValueError(f"provenance record missing required field: {name}")
        await self._sink.write(record)


def hash_payload(obj: Any) -> str | None:
    """Return a deterministic content fingerprint for ``obj``, or ``None`` for ``None``.

    The fingerprint is ``"sha256:" + hex digest`` of the canonical JSON
    encoding (sorted keys, compact separators), so it is independent of key
    order. The raw payload is never stored or returned — only its hash.
    """
    if obj is None:
        return None
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
