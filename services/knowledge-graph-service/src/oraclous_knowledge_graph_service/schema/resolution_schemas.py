"""Resolution request/response DTOs (ORAA-4 §21 schema layer — Pydantic only, no logic).

The HITL approve/reject contract for a `SAME_AS_CANDIDATE` pair (#279). `organisation_id` is never
an inbound field (ORG001) — it is resolved from the principal context. The candidate id is in the
path; the body carries the two endpoint node ids (the deterministic `id` properties the explorer
already holds), ordered so that on approve `canonical_node_id` is the survivor.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from oraclous_knowledge_graph_service.domain.resolution import (
    CandidatePair,
    MergeOutcome,
    RejectOutcome,
)


class ResolveCandidateRequest(BaseModel):
    """The pair to action. `canonical_node_id` is the node that SURVIVES an approve (the other is
    folded into it); for a reject the order is immaterial. Both are the nodes' deterministic `id`
    property as surfaced by the subgraph endpoint."""

    canonical_node_id: str = Field(min_length=1, max_length=128)
    other_node_id: str = Field(min_length=1, max_length=128)

    def to_pair(self) -> CandidatePair:
        # node_id_a is the canonical survivor (approve folds node_id_b onto node_id_a).
        return CandidatePair(node_id_a=self.canonical_node_id, node_id_b=self.other_node_id)


class ApproveResponse(BaseModel):
    """The surviving node + what changed, so the client can refresh the explorer."""

    candidate_id: str
    survivor_id: str
    merged_id: str
    repointed_edges: int
    aliases: list[str]

    @classmethod
    def of(cls, candidate_id: str, outcome: MergeOutcome) -> ApproveResponse:
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
