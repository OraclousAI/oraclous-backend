"""Organisation-scoped Redis cache keys (ORA-16 / A1, AC#4; generation #308).

Reshape of ``knowledge-graph-builder/app/services/query_cache_service.py``: the
legacy key ``qcache:{graph_id}:{sha256}`` (``graph_id`` the only tenant scope)
gains ``organisation_id`` as the *outermost* scope, becoming
``qcache:{organisation_id}:{graph_id}:{sha256}`` (ADR-006). The query is
normalised (lower/strip, lifted from the legacy ``_cache_key``) so whitespace
and case variants hit the same entry; the retriever type differentiates results.

#308 adds a per-graph *generation* segment so cross-service invalidation needs no
cross-service key-format coupling: the KGS bumps a neutral per-graph generation
counter (``graph_generation_key`` — a "graph version" signal over Redis, *not* the
retriever's private cache layout) on every successful ingest; the retriever folds
the current generation into ``query_cache_key`` so a fresh ingest leaves every
prior generation's entries naturally unreachable — a cache-miss, never an active
SCAN-and-delete reaching across the KGS↔retriever boundary.

Fail-closed: a blank organisation (or graph) raises rather than silently
producing an un-scoped key.
"""

from __future__ import annotations

import hashlib

_PREFIX = "qcache"
_GENERATION_PREFIX = "graphgen"


def _require(name: str, value: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required and must be non-blank (fail-closed, ADR-006)")
    return value


def query_cache_key(
    organisation_id: str,
    graph_id: str,
    query_text: str,
    retriever_type: str,
    generation: int = 0,
) -> str:
    """Build a deterministic, organisation-then-graph-scoped query cache key.

    ``generation`` is the per-graph version the KGS bumps on ingest; folding it into
    the hashed payload means a new generation produces a fresh key space, so every
    stale entry becomes a natural cache-miss without any active invalidation.
    """
    org = _require("organisation_id", organisation_id)
    graph = _require("graph_id", graph_id)
    normalised = query_text.lower().strip()
    payload = f"{org}|{graph}|{generation}|{normalised}|{retriever_type}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_PREFIX}:{org}:{graph}:{digest}"


def graph_generation_key(organisation_id: str, graph_id: str) -> str:
    """Neutral per-graph "version" key (#308) — the KGS↔retriever invalidation seam.

    The KGS bumps this counter (``INCR``) on every successful ingest; the retriever reads
    it and folds the value into ``query_cache_key``. It is a generic graph-version signal,
    not the retriever's private cache-key layout — so neither service has to know the
    other's key format. Organisation-then-graph scoped, fail-closed on a blank scope.
    """
    org = _require("organisation_id", organisation_id)
    graph = _require("graph_id", graph_id)
    return f"{_GENERATION_PREFIX}:{org}:{graph}"


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
