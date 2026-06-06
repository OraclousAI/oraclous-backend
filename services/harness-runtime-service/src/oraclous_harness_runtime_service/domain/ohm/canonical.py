"""OHM canonical serialisation + content hash (ORAA-4 §21 domain layer; OHM v1.0 spec §5).

The signed/hashed form of an OHM is the document with its ``signatures`` field removed, serialised
deterministically. We use canonical JSON (sorted keys, tight separators, UTF-8) rather than YAML
for the byte form — it is unambiguous, anchor-free, and trivially reproducible by any signer in any
language, which is what a signature needs. ``content_hash`` is the SHA-256 of those bytes (the OHM
artifact's immutable identity).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """Deterministic byte form of an OHM document for hashing/signing — excludes ``signatures``."""
    body = {k: v for k, v in document.items() if k != "signatures"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def content_hash(document: dict[str, Any]) -> str:
    """The OHM's content hash: SHA-256 hex of its canonical (signature-excluded) bytes."""
    return hashlib.sha256(canonical_bytes(document)).hexdigest()
