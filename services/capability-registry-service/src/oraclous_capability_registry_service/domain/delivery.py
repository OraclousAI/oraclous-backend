"""Clean-delta / idempotency core for deliver-back (#515, E6 / O7) — pure, substrate-agnostic.

The canonical O7 decision ("a recurring refresh writes a clean diff, NOT a clobber") is made by
Oraclous BEFORE any forge call, so it is identical across github/gitea: each file's content hashed
(``content_hash``); only files whose hash differs from the last delivery are written
(``changed_paths`` — never a clobber); and the whole delivery collapses to a stable ``delivery_key``
so an identical re-deliver dedupes to a NO_OP. The forge is only the git executor; this is where the
delta lives. Pure + I/O-free.
"""

from __future__ import annotations

import hashlib
import json
import uuid


def content_hash(content: bytes) -> str:
    """A stable, content-sensitive digest of a file's bytes (the per-file delta unit)."""
    return hashlib.sha256(content).hexdigest()


def delivery_key(
    *, organisation_id: str | uuid.UUID, repo: str, ref: str, file_hashes: dict[str, str]
) -> str:
    """The whole-delivery dedup key: order-independent over ``file_hashes`` and scope-sensitive on
    ``(organisation_id, repo, ref)`` — an identical re-deliver yields the SAME key (→ NO_OP), a
    changed file or a different org/repo/ref yields a DIFFERENT key (→ the delivery fires)."""
    canon = json.dumps(dict(sorted(file_hashes.items())), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(f"{organisation_id}\x00{repo}\x00{ref}\x00{canon}".encode()).hexdigest()


def changed_paths(incoming: dict[str, str], stored: dict[str, str]) -> list[str]:
    """The paths whose content hash differs from the last delivery (new or changed) — the minimal
    diff to write, never a clobber. An identical delivery yields ``[]`` (the NO_OP signal)."""
    return [path for path, h in incoming.items() if stored.get(path) != h]
