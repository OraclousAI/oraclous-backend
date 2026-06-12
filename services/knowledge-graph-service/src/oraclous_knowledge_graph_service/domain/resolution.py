"""Entity-resolution HITL domain (ORAA-4 §21 domain layer — pure, no I/O).

The entity-resolution pass (#269) flags ambiguous-band duplicate pairs as `SAME_AS_CANDIDATE`
edges between two canonical `:__Entity__` nodes for human review — flagged, not auto-merged. This
module is the pure core of the HITL *resolution* action (#279): the stable identity of a candidate
pair, the merge plan a human approval applies, and the value objects the service + repositories
exchange. No driver, no HTTP, no SQLAlchemy here.

candidate_id — the stable, opaque, **unordered** identity of a candidate pair. The frontend keys a
candidate on the two endpoint nodes' deterministic `id` properties; a `SAME_AS_CANDIDATE` edge is
direction-insensitive for review, so the id is `sha256(min(id)|max(id))`. It is stable across
reviewers (no per-session token) and across re-ingestion (the node ids are deterministic, the edge
MERGEs by endpoints), and it does not leak node ids in the URL.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class ResolutionAction(StrEnum):
    """The two HITL verdicts on a candidate pair."""

    APPROVE = "approve"  # merge: fold one node onto the canonical, delete the candidate edge.
    REJECT = "reject"  # not-duplicate: record a negative judgement, suppress, delete the edge.


class CandidateNotFound(Exception):
    """No live `SAME_AS_CANDIDATE` edge exists between the given pair in this graph (or it was
    already resolved). Maps to 404."""


class ResolutionConflict(Exception):
    """The pair was already resolved by another reviewer with a DIFFERENT verdict (concurrent
    review). Maps to 409 — the second reviewer must reload the queue, not silently override."""


def candidate_id(node_id_a: str, node_id_b: str) -> str:
    """Derive a candidate pair's stable, unordered id from its two endpoint node ids.

    `sha256(min|max)` so the id is identical regardless of which endpoint the caller passes first
    (a `SAME_AS_CANDIDATE` edge is undirected for review). The node ids are the deterministic node
    `id` property (sha256 over graph_id|label|key), so the candidate id is reproducible by either
    side without server state.
    """
    lo, hi = sorted((node_id_a.strip(), node_id_b.strip()))
    return hashlib.sha256(f"{lo}|{hi}".encode()).hexdigest()


@dataclass(frozen=True)
class CandidatePair:
    """The two endpoint node ids of a candidate pair (order-independent identity)."""

    node_id_a: str
    node_id_b: str

    def __post_init__(self) -> None:
        if not self.node_id_a.strip() or not self.node_id_b.strip():
            raise ValueError("a candidate pair needs two non-empty node ids")
        if self.node_id_a.strip() == self.node_id_b.strip():
            raise ValueError("a candidate pair must reference two distinct nodes")

    @property
    def candidate_id(self) -> str:
        return candidate_id(self.node_id_a, self.node_id_b)


@dataclass(frozen=True)
class MergeOutcome:
    """The result of an approve (merge): the surviving canonical node id + the count of edges
    re-pointed onto it (so the client can refresh the explorer). `aliases` is the surviving node's
    folded alias set after the merge."""

    survivor_id: str
    merged_id: str
    repointed_edges: int
    aliases: list[str]


@dataclass(frozen=True)
class RejectOutcome:
    """The result of a reject: the pair recorded not-duplicate (a `NOT_SAME_AS` suppression edge),
    the `SAME_AS_CANDIDATE` edge dropped so it leaves the review queue."""

    node_id_a: str
    node_id_b: str
    suppressed: bool
