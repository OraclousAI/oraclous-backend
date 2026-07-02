"""Curated wrong-method hints (domain layer) — a pure suggestion map for the proxy's 405 branch.

When a client uses the natural-but-wrong method on a resource (e.g. ``POST`` a GET-only collection
to "add" an item), the gateway's 405 branch surfaces one of these gateway-AUTHORED constant strings
in the ``METHOD_NOT_ALLOWED`` message, so the mistake self-corrects in ONE step (#579 / #440).

Leak-safe by construction: every value is a fixed constant — the request path/body is NEVER echoed
back (Interface Contracts §3 rule 8). This is a suggestion the gateway authors, NOT a relay of the
upstream response. The map is the generalization seam: a future GET-only-vs-POST-sibling pair adds
one entry, no code change. Mirrors ``domain/validation_passthrough.py``'s pure-function pattern.
"""

from __future__ import annotations

# (METHOD, resource-suffix) → curated hint. The suffix matches the END of the request path (a
# route-shape segment, never a tenant/id), so the returned string is a constant, not a reflection.
_SUGGESTIONS: dict[tuple[str, str], str] = {
    ("POST", "/documents"): (
        "Use POST /api/v1/graphs/{graphId}/upload (file) or /ingest (text) to add content; "
        "/documents is read-only (GET)."
    ),
}


def suggest_method_hint(method: str, path: str) -> str | None:
    """A curated 405 hint for a wrong-method guess on ``path``, or ``None`` when nothing matches (so
    the caller falls through to the generic 405). Matches on the path's trailing resource segment,
    so it never echoes tenant ids / the full path — the returned value is always a constant."""
    m = method.upper()
    trimmed = path.rstrip("/")
    for (hint_method, suffix), hint in _SUGGESTIONS.items():
        if m == hint_method and trimmed.endswith(suffix):
            return hint
    return None
