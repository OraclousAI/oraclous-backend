"""Pagination domain (domain layer) — bound every unbounded collection read (WP-10).

A collection endpoint accepts an OPTIONAL ``limit`` + ``offset``. Defaults are deliberately
backward-compatible: ``limit`` is optional and defaults GENEROUSLY (``DEFAULT_LIMIT``) so an
existing caller that passes neither is unaffected, and the response shape stays a plain list (no
``{items,total}`` envelope — the console consumes a bare array). ``limit`` is clamped at
``MAX_LIMIT`` so a caller can never ask the substrate for an unbounded page; the repository pairs
the bound with a stable ``ORDER BY`` so paging is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

# Generous default: existing callers that pass no params get up to this many rows (a chat
# transcript / an org's agents/keys/threads sit comfortably under it), so behaviour is unchanged.
DEFAULT_LIMIT = 100
# Hard ceiling: a caller-supplied limit above this is clamped, so no single read is unbounded.
MAX_LIMIT = 200


@dataclass(frozen=True)
class Pagination:
    """A resolved, already-bounded page window. ``limit`` is in ``[1, MAX_LIMIT]`` and ``offset``
    is ``>= 0`` by construction (the route dependency validates + clamps), so repositories apply it
    verbatim."""

    limit: int = DEFAULT_LIMIT
    offset: int = 0
