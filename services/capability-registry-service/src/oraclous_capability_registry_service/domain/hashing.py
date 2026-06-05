"""Content hashing (ORAA-4 §21 domain layer) — pure, no I/O.

A capability descriptor's ``content_hash`` is the SHA-256 of its canonical JSON form: None-valued
keys stripped (so a Pydantic ``model_dump()`` roundtrip hashes identically to the equivalent raw
dict) and keys sorted at every depth (so insertion order is irrelevant). This is the OHM hashing
contract; it lives in the service domain because the capability registry is the sole authority that
computes capability hashes (consumers read them via the API, never recompute).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def compute_content_hash(descriptor: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of the canonical JSON form of a descriptor."""
    canonical = json.dumps(_strip_none(descriptor), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
