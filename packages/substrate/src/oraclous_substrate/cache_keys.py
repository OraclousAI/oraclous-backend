"""Organisation-scoped Redis cache keys (ORA-16 / A1, AC#4).

Reshape of ``knowledge-graph-builder/app/services/query_cache_service.py``: the
legacy key ``qcache:{graph_id}:{sha256}`` (``graph_id`` the only tenant scope)
gains ``organisation_id`` as the *outermost* scope, becoming
``qcache:{organisation_id}:{graph_id}:{sha256}`` (ADR-006). The query is
normalised (lower/strip, lifted from the legacy ``_cache_key``) so whitespace
and case variants hit the same entry; the retriever type differentiates results.

Fail-closed: a blank organisation (or graph) raises rather than silently
producing an un-scoped key.
"""

from __future__ import annotations

import hashlib

_PREFIX = "qcache"


def _require(name: str, value: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required and must be non-blank (fail-closed, ADR-006)")
    return value


def query_cache_key(
    organisation_id: str, graph_id: str, query_text: str, retriever_type: str
) -> str:
    """Build a deterministic, organisation-then-graph-scoped query cache key."""
    org = _require("organisation_id", organisation_id)
    graph = _require("graph_id", graph_id)
    normalised = query_text.lower().strip()
    payload = f"{org}|{graph}|{normalised}|{retriever_type}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_PREFIX}:{org}:{graph}:{digest}"


def query_cache_pattern(organisation_id: str, graph_id: str | None = None) -> str:
    """Return a SCAN/glob pattern scoped to an organisation, optionally narrowed to a graph.

    ``query_cache_pattern(org)`` matches every key for that organisation;
    ``query_cache_pattern(org, graph)`` narrows to a single graph. Neither can
    match another organisation's keys, so org-scoped invalidation never reaches
    across the tenancy boundary.
    """
    org = _require("organisation_id", organisation_id)
    if graph_id is None:
        return f"{_PREFIX}:{org}:*"
    graph = _require("graph_id", graph_id)
    return f"{_PREFIX}:{org}:{graph}:*"
