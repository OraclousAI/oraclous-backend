"""Resolution request/response DTOs (ORAA-4 §21 schema layer — Pydantic only, no logic).

The HITL approve/reject contract for a `SAME_AS_CANDIDATE` pair (#279). `organisation_id` is never
an inbound field (ORG001) — it is resolved from the principal context. The candidate id is in the
path; the body carries the two endpoint node ids (the deterministic `id` properties the explorer
already holds), ordered so that on approve `canonical_node_id` is the survivor.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from oraclous_knowledge_graph_service.domain.resolution import (
    CandidatePair,
    CrossGraphCandidate,
    LinkOutcome,
    MergeOutcome,
    RejectOutcome,
)


class ResolveCandidateRequest(BaseModel):
    """The pair to action. `canonical_node_id` is the node that SURVIVES an approve (the other is
    folded into it); for a reject the order is immaterial. Both are the nodes' deterministic `id`
    property as surfaced by the subgraph endpoint.

    Cross-graph (#330): `other_graph_id` is the graph `other_node_id` lives in when it differs
    from the path graph (`canonical_node_id` always lives in the path graph). On a cross-graph
    approve the pair is LINKED (`SAME_AS`), never folded."""

    canonical_node_id: str = Field(min_length=1, max_length=128)
    other_node_id: str = Field(min_length=1, max_length=128)
    other_graph_id: uuid.UUID | None = None

    def to_pair(self) -> CandidatePair:
        # node_id_a is the canonical survivor (approve folds node_id_b onto node_id_a).
        return CandidatePair(node_id_a=self.canonical_node_id, node_id_b=self.other_node_id)


class ApproveResponse(BaseModel):
    """The surviving node + what changed, so the client can refresh the explorer. For a
    cross-graph approve (`linked=true`) nothing is folded: `survivor_id`/`merged_id` are the two
    linked nodes (path-graph node first), `repointed_edges` is 0 and `aliases` empty."""

    candidate_id: str
    survivor_id: str
    merged_id: str
    repointed_edges: int
    aliases: list[str]
    linked: bool = False

    @classmethod
    def of(cls, candidate_id: str, outcome: MergeOutcome | LinkOutcome) -> ApproveResponse:
        if isinstance(outcome, LinkOutcome):
            return cls(
                candidate_id=candidate_id,
                survivor_id=outcome.node_id_a,
                merged_id=outcome.node_id_b,
                repointed_edges=0,
                aliases=[],
                linked=outcome.linked,
            )
        return cls(
            candidate_id=candidate_id,
            survivor_id=outcome.survivor_id,
            merged_id=outcome.merged_id,
            repointed_edges=outcome.repointed_edges,
            aliases=outcome.aliases,
        )


class RejectResponse(BaseModel):
    candidate_id: str
    node_id_a: str
    node_id_b: str
    suppressed: bool

    @classmethod
    def of(cls, candidate_id: str, outcome: RejectOutcome) -> RejectResponse:
        return cls(
            candidate_id=candidate_id,
            node_id_a=outcome.node_id_a,
            node_id_b=outcome.node_id_b,
            suppressed=outcome.suppressed,
        )


class CrossGraphGenerateRequest(BaseModel):
    """Generate SAME_AS candidates between the path graph and `target_graph_id` (#330). Both must
    be the caller's graphs (cross-org → 404, fail-closed). `candidate_threshold` is the embedding
    cosine at/above which a non-identical name pair is flagged (the in-graph ambiguous-band floor);
    `limit` caps the pairs written per run."""

    target_graph_id: uuid.UUID
    candidate_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    limit: int = Field(default=100, ge=1, le=500)


class CrossGraphCandidateModel(BaseModel):
    """One flagged cross-graph pair: BOTH node ids and BOTH graph ids carried (ADR-026), plus the
    signal + score. `candidate_id` keys the same verdict endpoints the in-graph queue uses."""

    candidate_id: str
    node_id_a: str
    node_id_b: str
    graph_id_a: uuid.UUID
    graph_id_b: uuid.UUID
    label: str
    name_a: str
    name_b: str
    score: float
    method: str

    @classmethod
    def of(cls, c: CrossGraphCandidate) -> CrossGraphCandidateModel:
        return cls(
            candidate_id=c.candidate_id,
            node_id_a=c.node_id_a,
            node_id_b=c.node_id_b,
            graph_id_a=uuid.UUID(c.graph_id_a),
            graph_id_b=uuid.UUID(c.graph_id_b),
            label=c.label,
            name_a=c.name_a,
            name_b=c.name_b,
            score=c.score,
            method=c.method,
        )


class CrossGraphGenerateResponse(BaseModel):
    candidates: list[CrossGraphCandidateModel]
    generated: int
    warnings: list[str]


class PendingCrossGraphResponse(BaseModel):
    """The pending cross-graph SAME_AS review queue touching a graph (#330) — the read surface a
    HITL reviewer uses to see the candidates a generation run wrote. Same candidate shape; each
    pair keys the approve/reject verdict endpoints."""

    candidates: list[CrossGraphCandidateModel]
    total: int
