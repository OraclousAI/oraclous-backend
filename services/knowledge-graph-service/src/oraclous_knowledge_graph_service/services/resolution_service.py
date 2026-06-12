"""Entity-resolution HITL use-cases (ORAA-4 §21 services layer — all business logic, not in routes).

Actions a human verdict on a `SAME_AS_CANDIDATE` pair (#279): APPROVE merges the two canonical
entities into one (folding aliases + re-pointing relationships, deleting the candidate edge);
REJECT records a negative judgement (a `NOT_SAME_AS` suppression so the pair stops resurfacing) and
drops the edge. Both write a governance audit row (who/when/what).

Authz: gated on graph OWNERSHIP (the same owner gate graph CRUD uses) on top of the fail-closed
org-scope — a graph in another org/owner is invisible (404, no leak). Idempotency +
concurrent-reviewer handling: the audit table's `(org, graph, candidate)` unique key is the
arbiter — a replay of the SAME verdict returns the recorded outcome (no double mutation); a
DIFFERENT verdict by a second reviewer is a 409 conflict, never a silent override.
"""

from __future__ import annotations

import asyncio
import uuid

from oraclous_knowledge_graph_service.domain.resolution import (
    CandidateNotFound,
    CandidatePair,
    MergeOutcome,
    RejectOutcome,
    ResolutionAction,
    ResolutionConflict,
)
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)
from oraclous_knowledge_graph_service.repositories.resolution_repository import ResolutionRepository
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound, GraphService


class ResolutionService:
    """Approve / reject a candidate pair. Owner-gated, audited, idempotent."""

    def __init__(
        self,
        *,
        graph_service: GraphService,
        write_repo: GraphWriteRepository,
        audit_repo: ResolutionRepository,
    ) -> None:
        self._graphs = graph_service
        self._write = write_repo
        self._audit = audit_repo

    async def _owned_org(self, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> str:
        """Owner gate → the graph's organisation id as a string (for the Neo4j org-scoped calls).
        Raises GraphNotFound (→404) when the caller does not own the graph in their org."""
        graph = await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)
        return str(graph.organisation_id)

    async def approve(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        pair: CandidatePair,
        candidate_id_path: str,
    ) -> MergeOutcome:
        """Merge the pair: fold the non-canonical node onto the canonical survivor, delete the
        candidate edge. `pair.node_id_a` is treated as the canonical survivor (the client confirms
        which node survives by ordering the body); `node_id_b` is folded in."""
        org = await self._owned_org(graph_id=graph_id, user_id=user_id)
        self._assert_candidate_id(pair, candidate_id_path)

        prior = await self._audit.find(graph_id=graph_id, candidate_id=pair.candidate_id)
        if prior is not None:
            if prior.action != ResolutionAction.APPROVE.value:
                raise ResolutionConflict(pair.candidate_id)
            # Idempotent replay: the merge already happened; return the recorded survivor.
            return MergeOutcome(
                survivor_id=prior.canonical_node_id or pair.node_id_a,
                merged_id=pair.node_id_b
                if (prior.canonical_node_id or pair.node_id_a) == pair.node_id_a
                else pair.node_id_a,
                repointed_edges=0,
                aliases=[],
            )

        endpoints = await asyncio.to_thread(
            self._write.candidate_endpoints,
            graph_id=str(graph_id),
            organisation_id=org,
            node_id_a=pair.node_id_a,
            node_id_b=pair.node_id_b,
        )
        if endpoints is None:
            raise CandidateNotFound(pair.candidate_id)

        result = await asyncio.to_thread(
            self._write.merge_candidate,
            graph_id=str(graph_id),
            organisation_id=org,
            survivor_id=pair.node_id_a,
            merged_id=pair.node_id_b,
        )
        await self._audit.record(
            graph_id=graph_id,
            candidate_id=pair.candidate_id,
            node_id_a=pair.node_id_a,
            node_id_b=pair.node_id_b,
            action=ResolutionAction.APPROVE,
            canonical_node_id=pair.node_id_a,
            decided_by=user_id,
        )
        return MergeOutcome(
            survivor_id=result["survivor_id"],
            merged_id=pair.node_id_b,
            repointed_edges=result["repointed_edges"],
            aliases=result["aliases"],
        )

    async def reject(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        pair: CandidatePair,
        candidate_id_path: str,
    ) -> RejectOutcome:
        """Reject the pair: record a NOT_SAME_AS negative judgement (suppress from future candidate
        lists) and drop the candidate edge."""
        org = await self._owned_org(graph_id=graph_id, user_id=user_id)
        self._assert_candidate_id(pair, candidate_id_path)

        prior = await self._audit.find(graph_id=graph_id, candidate_id=pair.candidate_id)
        if prior is not None:
            if prior.action != ResolutionAction.REJECT.value:
                raise ResolutionConflict(pair.candidate_id)
            return RejectOutcome(
                node_id_a=pair.node_id_a, node_id_b=pair.node_id_b, suppressed=True
            )

        endpoints = await asyncio.to_thread(
            self._write.candidate_endpoints,
            graph_id=str(graph_id),
            organisation_id=org,
            node_id_a=pair.node_id_a,
            node_id_b=pair.node_id_b,
        )
        if endpoints is None:
            raise CandidateNotFound(pair.candidate_id)

        suppressed = await asyncio.to_thread(
            self._write.suppress_candidate,
            graph_id=str(graph_id),
            organisation_id=org,
            node_id_a=pair.node_id_a,
            node_id_b=pair.node_id_b,
        )
        await self._audit.record(
            graph_id=graph_id,
            candidate_id=pair.candidate_id,
            node_id_a=pair.node_id_a,
            node_id_b=pair.node_id_b,
            action=ResolutionAction.REJECT,
            canonical_node_id=None,
            decided_by=user_id,
        )
        return RejectOutcome(
            node_id_a=pair.node_id_a, node_id_b=pair.node_id_b, suppressed=suppressed
        )

    @staticmethod
    def _assert_candidate_id(pair: CandidatePair, candidate_id_path: str) -> None:
        """The path's candidate id must be the canonical hash of the body's node pair — so the URL
        identity and the operands cannot disagree (a malformed/forged path id is a 404, never an
        action on a different pair)."""
        if pair.candidate_id != candidate_id_path:
            raise CandidateNotFound(candidate_id_path)


__all__ = [
    "CandidateNotFound",
    "GraphNotFound",
    "ResolutionConflict",
    "ResolutionService",
]
