"""Freezing a team's documents into an app (domain layer, #932).

An app carries its own copy of the team's documents. For an Oraclous-provided app that copy is read
by EVERY organisation, so the one thing freezing must guarantee is that nothing belonging to the
author survives into it — no model credential id, no tool credential mapping.

A leaked credential id is not directly exploitable: the broker resolves one org-scoped, so another
tenant presenting it gets nothing. The leak is that it is one organisation's identifier sitting in a
row every other organisation can read, and that is reason enough. The scrub is therefore driven by
the KEY NAME wherever it appears, not by the two shapes we happen to know about today
(``models[].config`` and ``capabilities[].config``) — a manifest that grows a third home must not
quietly start publishing credentials.

What survives is deliberate: the model BINDING stays, because the app screen says what it was built
for and a run rebinds the caller's own key onto that same binding. Unrelated settings under the same
``config`` stay too — this removes credentials, not configuration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: Key names that carry an author's credentials. Removed wherever they appear, at any depth.
#: ``credential_id`` is a model's BYOM key (ADR-008); ``credential_mappings`` is a tool instance's.
_CREDENTIAL_KEYS = frozenset({"credential_id", "credential_mappings"})


@dataclass(frozen=True)
class FrozenApp:
    """The documents an app stores, plus the fingerprint the startup seed compares."""

    manifest: dict[str, Any]
    sub_harnesses: dict[str, dict[str, Any]]
    fingerprint: str


def _scrub(value: Any) -> Any:
    """Deep-copy ``value``, dropping every credential-bearing key at any depth.

    Copying and scrubbing are one pass on purpose: a two-pass version (copy, then edit) leaves a
    window in which a caller could hold the un-scrubbed copy, and invites someone to later reuse
    the copy half alone.
    """
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _CREDENTIAL_KEYS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def fingerprint_documents(
    manifest: dict[str, Any], sub_harnesses: dict[str, dict[str, Any]]
) -> str:
    """A sha256 over the canonical documents.

    ``sort_keys`` is what makes a re-seed a no-op: the seed re-reads a committed JSON file on every
    boot, and a reordering that changes nothing must not read as a change and rewrite a live app.
    """
    canonical = json.dumps(
        {"manifest": manifest, "sub_harnesses": sub_harnesses},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def freeze_documents(
    manifest: dict[str, Any], sub_harnesses: dict[str, dict[str, Any]]
) -> FrozenApp:
    """Copy ``manifest`` + ``sub_harnesses`` into what an app stores, scrubbed of credentials.

    Never edits in place: the caller's documents are a live draft the rest of the request still
    reads, and an app freezing them must not reach back and change what the caller is holding.
    """
    frozen_manifest = _scrub(manifest)
    frozen_subs = _scrub(sub_harnesses)
    return FrozenApp(
        manifest=frozen_manifest,
        sub_harnesses=frozen_subs,
        fingerprint=fingerprint_documents(frozen_manifest, frozen_subs),
    )
