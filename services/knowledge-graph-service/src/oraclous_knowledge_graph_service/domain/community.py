"""Community-detection domain layer (ORAA-4 §21 domain layer — pure, no I/O) (#303).

Restores the legacy hierarchical community capability
(``knowledge-graph-builder/app/tasks/community_tasks.py`` +
``services/community_summarizer.py`` + ``analytics_service.py``), RE-ARCHITECTED onto in-DB Neo4j
GDS Louvain: the in-memory ``leidenalg``/``igraph`` pipeline is gone — community detection now runs
as Cypher ``CALL gds.louvain.*`` inside Neo4j (the repositories layer). Leiden is Enterprise-only
so it is deliberately NOT used; Louvain is the Community-Edition algorithm.

This module holds only the pure pieces: the deterministic community-id scheme (matched to the legacy
16-char SHA-256 contract), the native-dendrogram → level mapping, value objects, and the typed
errors. No driver, no Cypher, no LLM — those live in ``repositories.community_repository`` /
``services.analytics_service`` / ``services.community_summarizer``.

The hierarchy = GDS Louvain's NATIVE dendrogram (no resolution sweep)
====================================================================
GDS Louvain on Community Edition has NO ``resolution``/``gamma`` parameter (that knob is
Leiden/Enterprise). The earlier port faked a hierarchy by re-running Louvain at five
weight-contrast exponents (``w ** resolution``) — but with uniform edge weights (the dominant case:
the only system weight is ``len(rels)``, almost always 1, and ``1.0 ** r == 1.0``) every exponent
yields the IDENTICAL partition, so that produced five duplicate levels chained by meaningless parent
edges. That sweep is gone.

Instead detection runs ONE ``gds.louvain.stream`` with ``includeIntermediateCommunities: true`` and
maps the per-iteration dendrogram each node carries (``intermediateCommunityIds``) onto the
``:__Community__`` levels — one level per dendrogram depth, exactly as deep as Louvain actually
converged. A live probe on ``neo4j:5.23-community`` (GDS 2.11) established the array ordering:
``intermediateCommunityIds[0]`` is the FINEST partition (most communities) and the LAST element is
the COARSEST (== the row's ``communityId``); a planted weight-decay hierarchy gave ``[1,3]`` for the
fine pair and ``[3,3]`` for its sibling, i.e. index 0 = 8 communities, index 1 = 4 communities.

``level`` is assigned so 0 is the COARSEST (matching the legacy ``DEFAULT_LEVELS`` ordering, where
level 0 is parent-less): ``level = (depth - 1) - dendrogram_index``. Parent links are the ACTUAL
dendrogram containment read straight off the array (a node's level-k community ⊂ its level-(k-1)
community) — NOT a majority vote, so the hierarchy is monotone by construction. When Louvain
converges flat (depth 1) exactly ONE honest level is emitted, with no fabricated parents.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

# A graph smaller than this many entities is not worth detecting communities over (mirrors the
# legacy COMMUNITY_DETECTION_MIN_ENTITIES default). The detect use-case treats it as a skip, not an
# error.
DEFAULT_MIN_ENTITIES: int = 3

# Community node / membership-edge labels — the legacy unified-model contract (kept verbatim so the
# explorer and any legacy reader see the same shapes).
COMMUNITY_LABEL = "__Community__"
ENTITY_LABEL = "__Entity__"
IN_COMMUNITY_REL = "IN_COMMUNITY"
PARENT_COMMUNITY_REL = "PARENT_COMMUNITY"

# The synthetic ``ingestion_jobs.source_type`` for an async community-detection job. Reuses the
# existing job table + worker pattern (no new migration) — the row tracks detect progress/status,
# and is filtered out of the /documents list (it is not an ingested document).
COMMUNITY_DETECT_SOURCE_TYPE = "community_detect"

# The only community kind this service detects (entity-level Louvain). The legacy registry also
# carried a read-only chunk kind; that is out of scope for the restoration and is reported as a
# non-detectable kind by the discovery endpoint if ever added.
ENTITY_KIND = "entity"


class CommunityDetectionError(Exception):
    """Base for detection failures that map to a clean HTTP error (not a swallowed 500)."""


class DetectionInProgress(CommunityDetectionError):
    """Another detection run already holds the per-(org,graph) lock.

    Raised by the repository when the Redis detect lock is held for this (org, graph): a concurrent
    detect is mid clear+rebuild, so a second run is refused rather than allowed to race the
    destructive rebuild. The service maps it to an "already in progress" result (HTTP 202/skip).
    """


class GdsUnavailableError(CommunityDetectionError):
    """The Neo4j GDS procedures are not installed/loadable (``gds.*`` missing).

    Raised by the repository when a ``gds.louvain``/``gds.graph.project`` call fails because the
    plugin is absent, so the service maps it to a clear 503 ("community detection unavailable: the
    Neo4j Graph Data Science plugin is not loaded") rather than a generic 500. Fail-closed and
    typed — the failure mode is observable, never silently swallowed.
    """


def make_community_id(*, graph_id: str, level: int, member_ids: list[str]) -> str:
    """Deterministic ``community_<16-hex>`` id (legacy 16-char SHA-256 scheme).

    The id is a SHA-256 over ``graph | level | sorted-members`` (the native dendrogram has no
    resolution knob, so resolution is no longer part of the identity). The same set of members at
    the same level always yields the same id (idempotent re-detection MERGEs onto the existing node)
    and a different membership yields a different id. Sorting makes the id order-independent.
    """
    sorted_ids = sorted(member_ids)
    content = f"{graph_id}|L{level}|" + "|".join(sorted_ids)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"community_{digest}"


def _normalise_dendrogram(intermediate: list[int], *, depth: int) -> list[int]:
    """Pad a SHORT dendrogram row on the COARSE end to the global ``depth``.

    GDS normally emits one entry per node for every iteration, so every row has the same length; but
    a ragged dendrogram (rows of differing length) must not crash detection or strand a short row's
    members below the coarsest level. Since the array runs ``[finest, ..., coarsest]``, a short row
    is normalised by repeating its COARSEST id (the last element) up the missing coarser slots —
    i.e. a node that stopped subdividing early is treated as belonging to that same community at
    every coarser level. This keeps the partition complete (every entity reaches level 0) and the
    parent chain monotone (no IndexError reading ``intermediate[idx + 1]``)."""
    if len(intermediate) >= depth:
        return intermediate
    coarsest = intermediate[-1]
    return intermediate + [coarsest] * (depth - len(intermediate))


def dendrogram_to_levels(
    rows: list[tuple[str, list[int]]],
) -> dict[int, dict[str, list[str]]]:
    """Map GDS Louvain's native dendrogram → ``level → {gds_community_key: [entity_id, ...]}``.

    ``rows`` is one ``(entity_id, intermediate_community_ids)`` per node, exactly as streamed by
    ``gds.louvain.stream(..., {includeIntermediateCommunities: true})``. Per the live-verified
    ordering, ``intermediate_community_ids[0]`` is the FINEST partition and the last element is the
    COARSEST. The dendrogram depth is the MAX array length (Louvain normally emits one entry per
    node for every iteration it ran; a ragged dendrogram — rows of differing length — is normalised
    by padding short rows on the coarse end, see :func:`_normalise_dendrogram`, so every entity
    still reaches the coarsest level and nothing is stranded).

    Levels are numbered so 0 is the COARSEST (legacy ``DEFAULT_LEVELS`` ordering, where level 0 is
    parent-less): ``level = (depth - 1) - dendrogram_index``. The gds community KEY at a level is
    made unique per level (``"L<level>:<gds_id>"``) because GDS reuses the same integer id across
    iterations — without the level prefix, a node unchanged between iterations would collide its
    finer and coarser communities into one key.

    Returns ``{}`` when ``rows`` is empty. When the dendrogram is depth 1 (Louvain converged flat)
    exactly one level (level 0) is emitted — one honest level, no fabricated hierarchy.
    """
    if not rows:
        return {}
    depth = max(len(ids) for _eid, ids in rows)
    if depth == 0:
        return {}
    levels: dict[int, dict[str, list[str]]] = {}
    for entity_id, intermediate in rows:
        if not intermediate:
            continue
        for idx, gds_id in enumerate(_normalise_dendrogram(intermediate, depth=depth)):
            level = (depth - 1) - idx
            key = f"L{level}:{gds_id}"
            levels.setdefault(level, {}).setdefault(key, []).append(entity_id)
    return levels


def dendrogram_parent_links(
    rows: list[tuple[str, list[int]]],
) -> dict[int, dict[str, str | None]]:
    """Parent links read DIRECTLY off the dendrogram (true containment, not majority vote).

    For each node, its community at dendrogram index ``i`` (finer) is by construction contained in
    its community at index ``i+1`` (coarser). Translating to levels: a level-``k`` community's
    parent is the level-``(k-1)`` community the same members belong to. The coarsest level (level 0)
    has no parent. Keys match :func:`dendrogram_to_levels` (``"L<level>:<gds_id>"``). Short (ragged)
    rows are padded on the coarse end first (see :func:`_normalise_dendrogram`), so ``idx + 1`` is
    always in range and no parent link is dropped. Returns ``level → {child_key: parent_key|None}``.
    """
    if not rows:
        return {}
    depth = max(len(ids) for _eid, ids in rows)
    parents: dict[int, dict[str, str | None]] = {}
    for _entity_id, intermediate in rows:
        if not intermediate:
            continue
        normalised = _normalise_dendrogram(intermediate, depth=depth)
        for idx, gds_id in enumerate(normalised):
            level = (depth - 1) - idx
            child_key = f"L{level}:{gds_id}"
            if level == 0:
                parents.setdefault(level, {}).setdefault(child_key, None)
                continue
            # The coarser community is the NEXT element in the array (idx + 1 → level - 1).
            parent_gds = normalised[idx + 1]
            parent_key = f"L{level - 1}:{parent_gds}"
            parents.setdefault(level, {})[child_key] = parent_key
    return parents


@dataclass(frozen=True)
class CommunityMember:
    """One entity belonging to a community (for the detail view)."""

    entity_id: str
    entity_name: str
    entity_type: str


@dataclass(frozen=True)
class Community:
    """A detected community node, as read back from Neo4j (the list/detail view shape).

    ``summary``/``summary_keywords``/``summary_excerpt`` are populated by the summarizer pass (None
    until summarised). ``parent_id`` links a finer-level community to its coarser-level parent — the
    ACTUAL dendrogram containment (the coarser community the same members collapse into), not a
    majority vote — reproducing the PARENT_COMMUNITY hierarchy monotonically by construction.
    ``summary_source`` distinguishes a real LLM summary (``"llm"``) from a member-derived fallback
    (``"fallback"``) so a reader never mistakes a degraded summary for a real one.
    """

    community_id: str
    kind: str
    level: int
    entity_count: int
    status: str
    weight: float | None = None
    parent_id: str | None = None
    summary: str | None = None
    summary_keywords: list[str] | None = None
    summary_excerpt: str | None = None
    summary_model: str | None = None
    summary_at: datetime | None = None
    summary_source: str | None = None
    members: list[CommunityMember] = field(default_factory=list)


@dataclass(frozen=True)
class DetectionResult:
    """The outcome of a detection run (returned to the caller / job result)."""

    graph_id: str
    status: str  # "completed" | "skipped"
    total_communities: int
    communities_per_level: dict[int, int]
    entities_processed: int
    reason: str | None = None


@dataclass(frozen=True)
class CommunitiesStatus:
    """Detection status for a graph (mirrors the legacy ``/communities/status`` shape).

    Derived from the graph's ``:__Community__`` nodes + current entity count, FOLDED WITH the latest
    ``community_detect`` job row so an in-flight or failed async run is visible (the substrate alone
    shows ``not_detected`` mid-run, right after the clear): ``running`` when the latest detect job
    is pending/running, ``failed`` when it errored and no communities exist, ``not_detected`` when
    no community nodes and no job, else ``active`` — plus a staleness signal from the entity delta.
    """

    graph_id: str
    status: str  # "not_detected" | "running" | "failed" | "active"
    communities_count: int
    levels: list[int]
    entity_count: int
    entity_count_at_detection: int
    is_stale: bool


@dataclass(frozen=True)
class GraphAnalytics:
    """Graph-level summary statistics (mirrors the legacy ``/analytics`` shape).

    Entity/relationship counts, label + relationship-type breakdowns, density, average degree, and
    the top entities by degree — all org+graph scoped, read live from Neo4j.
    """

    graph_id: str
    node_count: int
    relationship_count: int
    entity_count: int
    density: float
    avg_degree: float
    entity_types: list[dict[str, object]]
    relationship_types: list[dict[str, object]]
    top_entities: list[dict[str, object]]
    community_count: int
    computed_at: datetime


@dataclass(frozen=True)
class CommunityKind:
    """A community kind the platform can surface (the discovery endpoint shape)."""

    kind: str
    display_name: str
    community_label: str
    member_label: str
    hierarchical: bool
    detection_supported: bool


def entity_kinds() -> list[CommunityKind]:
    """The community-kind registry. Today the only kind is entity-level Louvain (hierarchical,
    detectable). Kept as a function (not a const) so a future read-only kind can be appended without
    a breaking change to callers."""
    return [
        CommunityKind(
            kind=ENTITY_KIND,
            display_name="Entity communities",
            community_label=COMMUNITY_LABEL,
            member_label=ENTITY_LABEL,
            hierarchical=True,
            detection_supported=True,
        )
    ]
