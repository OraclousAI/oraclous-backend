"""Similarity-based memory consolidation (ORAA-4 §21 domain layer — pure, no I/O).

Issue #332 / ADR-027 §3. The legacy consolidation was hash-dedup pretending to be similarity; this
is the real thing: memories whose stored embeddings sit above a cosine threshold cluster together
and merge — the highest-importance member wins, absorbing the losers' importance (capped at 1.0).

Pure functions over already-fetched ``(memory_id, embedding, importance)`` rows so the clustering
is unit-testable with controlled vectors; the repository applies the resulting merge plan in Cypher
and the Celery task (``tasks/memory_tasks.py``) holds the per-(org,graph) advisory lock around the
whole pass (#303/#305 pattern).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MemoryVector:
    """One consolidation candidate: a current memory with a stored embedding.

    ``partition`` is the (memory_type, scope, agent_id) tuple consolidation clusters WITHIN — two
    candidates may only merge when their partitions are identical, so a semantic never absorbs an
    episodic, an organisation-scoped memory is never invalidated by a session/agent-scoped one, and
    agent A never absorbs agent B (#332 HIGH-1)."""

    memory_id: str
    embedding: tuple[float, ...]
    importance: float  # base_importance — the quantity the winner absorbs
    partition: tuple[str, str, str] = ("", "", "")


@dataclass(frozen=True)
class MergeCluster:
    """A planned merge: ``winner`` absorbs ``losers`` (all above the similarity threshold)."""

    winner_id: str
    loser_ids: tuple[str, ...]
    merged_importance: float  # winner + sum(losers), capped at 1.0
    members: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", 1 + len(self.loser_ids))


def cosine(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> float:
    """Plain cosine similarity; 0.0 when either vector is zero or the dims mismatch."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def cluster_by_similarity(items: list[MemoryVector], *, threshold: float) -> list[MergeCluster]:
    """Greedy importance-first clustering, PARTITIONED by (memory_type, scope, agent_id).

    Candidates are first split into partitions; clustering only ever happens WITHIN a partition, so
    two memories of different type/scope/agent can never merge regardless of vector similarity
    (#332 HIGH-1). Within a partition: walk candidates by importance (desc); each unassigned item
    seeds a cluster and absorbs every later unassigned item whose cosine to the SEED is at or above
    ``threshold``. Seed-anchored (not transitive) so a chain of pairwise-similar memories cannot
    drift the cluster away from its winner. Returns only clusters that actually merge (≥ 2 members).
    Deterministic: partitions and ties in importance break on memory_id."""
    partitions: dict[tuple[str, str, str], list[MemoryVector]] = {}
    for item in items:
        partitions.setdefault(item.partition, []).append(item)

    clusters: list[MergeCluster] = []
    for _partition in sorted(partitions):
        ordered = sorted(partitions[_partition], key=lambda v: (-v.importance, v.memory_id))
        assigned: set[str] = set()
        for i, seed in enumerate(ordered):
            if seed.memory_id in assigned:
                continue
            assigned.add(seed.memory_id)
            losers: list[str] = []
            merged = seed.importance
            for other in ordered[i + 1 :]:
                if other.memory_id in assigned:
                    continue
                if cosine(seed.embedding, other.embedding) >= threshold:
                    assigned.add(other.memory_id)
                    losers.append(other.memory_id)
                    merged += other.importance
            if losers:
                clusters.append(
                    MergeCluster(
                        winner_id=seed.memory_id,
                        loser_ids=tuple(losers),
                        merged_importance=min(1.0, merged),
                    )
                )
    return clusters
