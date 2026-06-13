"""Cross-graph SAME_AS candidate generation (ORAA-4 §21 services layer) — #330 / ADR-026.

Given the canonical entities of TWO org-owned graphs, flag the pairs that look like the same
real-world entity — flagged, NEVER auto-merged: each pair becomes a `SAME_AS_CANDIDATE` edge in
the EXISTING HITL review pipeline (#279), carrying BOTH graph ids, and a human verdict
(approve → SAME_AS link / reject → NOT_SAME_AS suppression) actions it through the same audited
endpoints. This deliberately does NOT port the legacy 1k-line `EntityResolver`; it reuses the
in-graph resolution pass's signals at the graph-pair boundary:

  1. CANONICAL-KEY match (deterministic): two entities with the SAME label and the SAME canonical
     key (`name` — the key the resolve-on-write pass stamped) in different graphs. Score 1.0.
  2. EMBEDDING similarity (semantic, fail-soft): per label, embed the DISTINCT canonical names of
     both graphs with the configured embedder (the same `make_embedder` seam the in-graph pass
     uses), L2-normalise, pairwise cosine ACROSS the graph pair; flag pairs at/above
     `candidate_threshold`. An embed() failure skips ONLY this stage — deterministic matches
     stand (the embedder-off degrade).

Always per-label (the in-graph over-merge guard), always a graph-A×graph-B pair (never within one
graph — that is the in-graph pass's job), deterministic order in and out.
"""

from __future__ import annotations

import logging
import math

from oraclous_knowledge_graph_service.domain.resolution import CrossGraphCandidate

logger = logging.getLogger(__name__)

_SCORE_DP = 4  # round the cosine carried on a candidate to this many decimal places


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return list(vector)
    return [v / norm for v in vector]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def _by_label(entities: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for ent in entities:
        grouped.setdefault(ent["label"], []).append(ent)
    return grouped


def generate_cross_graph_pairs(
    *,
    graph_id_a: str,
    entities_a: list[dict],
    graph_id_b: str,
    entities_b: list[dict],
    candidate_threshold: float,
    embedder,
    limit: int,
    skip_pairs: set[tuple[str, str]] | None = None,
) -> tuple[list[CrossGraphCandidate], list[str]]:
    """Flag cross-graph candidate pairs between two entity sets. Returns ``(candidates,
    warnings)`` — warnings carry the fail-soft embedder skips. Each entity dict carries
    ``id/name/canonical_name/label`` (the `cross_graph_entities` repository shape).

    ``skip_pairs`` is the set of canonicalised ``(lo_id, hi_id)`` node-id pairs a human has already
    resolved (approved/rejected). They are dropped BEFORE the ``limit`` budget is spent, so an
    already-verdicted pair neither resurfaces in the response, over-counts ``generated``, nor
    consumes a slot a still-undecided pair could take. Seeding ``seen`` with them is enough — every
    candidate keys on the same canonicalised pair tuple."""
    candidates: list[CrossGraphCandidate] = []
    seen: set[tuple[str, str]] = set(skip_pairs or ())
    warnings: list[str] = []

    grouped_a = _by_label(entities_a)
    grouped_b = _by_label(entities_b)

    for label in sorted(set(grouped_a) & set(grouped_b)):
        side_a = sorted(grouped_a[label], key=lambda e: (e["name"], e["id"]))
        side_b = sorted(grouped_b[label], key=lambda e: (e["name"], e["id"]))

        # Stage 1 — deterministic canonical-key match (always runs; embedding-free).
        b_by_key: dict[str, list[dict]] = {}
        for ent in side_b:
            b_by_key.setdefault(ent["name"], []).append(ent)
        for ent_a in side_a:
            for ent_b in b_by_key.get(ent_a["name"], []):
                pair_key = tuple(sorted((ent_a["id"], ent_b["id"])))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                candidates.append(
                    CrossGraphCandidate(
                        node_id_a=ent_a["id"],
                        node_id_b=ent_b["id"],
                        graph_id_a=graph_id_a,
                        graph_id_b=graph_id_b,
                        label=label,
                        name_a=ent_a["canonical_name"],
                        name_b=ent_b["canonical_name"],
                        score=1.0,
                        method="canonical_key",
                    )
                )

        # Stage 2 — embedding cosine across the pair (fail-soft per label).
        names_a = sorted({e["name"] for e in side_a})
        names_b = sorted({e["name"] for e in side_b})
        if not names_a or not names_b:
            continue
        try:
            vectors = embedder.embed(names_a + names_b)
        except Exception:  # noqa: BLE001 — fail-soft: deterministic matches stand.
            logger.warning(
                "cross-graph resolution: embedder failed for label %r; semantic stage skipped "
                "(canonical-key matches still apply).",
                label,
                exc_info=True,
            )
            warnings.append(
                f"cross-graph resolution: embedder failed for label {label!r}; "
                "semantic stage skipped."
            )
            continue
        normalized = [_l2_normalize(v) for v in vectors]
        vec_a = dict(zip(names_a, normalized[: len(names_a)], strict=True))
        vec_b = dict(zip(names_b, normalized[len(names_a) :], strict=True))
        a_by_key: dict[str, list[dict]] = {}
        for ent in side_a:
            a_by_key.setdefault(ent["name"], []).append(ent)
        for name_a in names_a:
            for name_b in names_b:
                if name_a == name_b:
                    continue  # already covered by the deterministic stage
                score = _dot(vec_a[name_a], vec_b[name_b])
                if score < candidate_threshold:
                    continue
                for ent_a in a_by_key[name_a]:
                    for ent_b in b_by_key.get(name_b, []):
                        pair_key = tuple(sorted((ent_a["id"], ent_b["id"])))
                        if pair_key in seen:
                            continue
                        seen.add(pair_key)
                        candidates.append(
                            CrossGraphCandidate(
                                node_id_a=ent_a["id"],
                                node_id_b=ent_b["id"],
                                graph_id_a=graph_id_a,
                                graph_id_b=graph_id_b,
                                label=label,
                                name_a=ent_a["canonical_name"],
                                name_b=ent_b["canonical_name"],
                                score=round(score, _SCORE_DP),
                                method="embedding",
                            )
                        )

    # Deterministic order out: strongest signal first, then the stable pair identity.
    candidates.sort(key=lambda c: (-c.score, c.node_id_a, c.node_id_b))
    return candidates[:limit], warnings
