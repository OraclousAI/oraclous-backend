"""Entity-resolution HITL use-cases (services layer — all business logic, not in routes).

Actions a human verdict on a `SAME_AS_CANDIDATE` pair (#279): APPROVE merges the two canonical
entities into one (folding aliases + re-pointing relationships, deleting the candidate edge);
REJECT records a negative judgement (a `NOT_SAME_AS` suppression so the pair stops resurfacing) and
drops the edge. Both write a governance audit row (who/when/what).

Cross-graph extension (#330 / ADR-026): `generate_cross_graph` flags candidate pairs ACROSS an
org-owned graph PAIR (canonical-key match + embedding similarity — `cross_graph_resolution`),
writing the same `SAME_AS_CANDIDATE` edges with BOTH graph ids carried. The SAME verdict
endpoints action them: a cross-graph APPROVE LINKS (`SAME_AS`) instead of folding — nodes stay
in their own graphs — and a cross-graph REJECT suppresses with a both-ids `NOT_SAME_AS`. Both
graphs are owner-gated; a graph in another org is invisible (404), so a cross-ORG pair is
impossible by construction. Same audit table, same idempotency/conflict rules.

Authz: gated on graph OWNERSHIP (the same owner gate graph CRUD uses) on top of the fail-closed
org-scope — a graph in another org/owner is invisible (404, no leak). Idempotency +
concurrent-reviewer handling: the audit table's `(org, graph, candidate)` unique key is the
arbiter — a replay of the SAME verdict returns the recorded outcome (no double mutation); a
DIFFERENT verdict by a second reviewer is a 409 conflict, never a silent override.
"""

from __future__ import annotations

import asyncio
import uuid

from oraclous_knowledge_graph_service.core.config import Settings, get_settings
from oraclous_knowledge_graph_service.domain.resolution import (
    CandidateNotFound,
    CandidatePair,
    CrossGraphCandidate,
    LinkOutcome,
    MergeOutcome,
    RejectOutcome,
    ResolutionAction,
    ResolutionConflict,
)
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)
from oraclous_knowledge_graph_service.repositories.resolution_repository import ResolutionRepository
from oraclous_knowledge_graph_service.services.cross_graph_resolution import (
    generate_cross_graph_pairs,
)
from oraclous_knowledge_graph_service.services.embedder import make_embedder
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound, GraphService

# The most entities fetched per graph for cross-graph candidate generation (a bounded scan).
_CROSS_GRAPH_ENTITY_SCAN_LIMIT = 5000


class ResolutionService:
    """Approve / reject a candidate pair. Owner-gated, audited, idempotent."""

    def __init__(
        self,
        *,
        graph_service: GraphService,
        write_repo: GraphWriteRepository,
        audit_repo: ResolutionRepository,
        settings: Settings | None = None,
    ) -> None:
        self._graphs = graph_service
        self._write = write_repo
        self._audit = audit_repo
        self._settings = settings or get_settings()

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
        other_graph_id: uuid.UUID | None = None,
    ) -> MergeOutcome | LinkOutcome:
        """Merge the pair: fold the non-canonical node onto the canonical survivor, delete the
        candidate edge. `pair.node_id_a` is treated as the canonical survivor (the client confirms
        which node survives by ordering the body); `node_id_b` is folded in.

        Cross-graph (#330): when `other_graph_id` names a DIFFERENT graph (the graph `node_id_b`
        lives in), the approve LINKS instead of folding — a `SAME_AS` edge carrying both graph
        ids — because a fold would move a node out of its graph. Both graphs are owner-gated."""
        if other_graph_id is not None and other_graph_id != graph_id:
            return await self._approve_cross_graph(
                graph_id=graph_id,
                other_graph_id=other_graph_id,
                user_id=user_id,
                pair=pair,
                candidate_id_path=candidate_id_path,
            )
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
        other_graph_id: uuid.UUID | None = None,
    ) -> RejectOutcome:
        """Reject the pair: record a NOT_SAME_AS negative judgement (suppress from future candidate
        lists) and drop the candidate edge. Cross-graph (#330): when `other_graph_id` names a
        different graph, the suppression edge carries BOTH graph ids and both graphs are
        owner-gated; re-generation then skips the pair."""
        if other_graph_id is not None and other_graph_id != graph_id:
            return await self._reject_cross_graph(
                graph_id=graph_id,
                other_graph_id=other_graph_id,
                user_id=user_id,
                pair=pair,
                candidate_id_path=candidate_id_path,
            )
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

    # ── cross-graph SAME_AS (#330 / ADR-026) ───────────────────────────────────────────────────

    async def generate_cross_graph(
        self,
        *,
        graph_id: uuid.UUID,
        target_graph_id: uuid.UUID,
        user_id: uuid.UUID,
        candidate_threshold: float,
        limit: int,
    ) -> tuple[list[CrossGraphCandidate], list[str]]:
        """Flag SAME_AS candidates between TWO org-owned graphs and write them into the existing
        review queue (`SAME_AS_CANDIDATE` edges, BOTH graph ids carried). Returns
        ``(candidates, warnings)``. Both graphs are owner-gated (cross-org → 404 by construction).
        Pairs a human already resolved (NOT_SAME_AS / SAME_AS) are filtered out of the RESPONSE
        (and so don't over-count `generated`) BEFORE the limit budget is spent — not just skipped
        at the write. The quadratic-cosine + sync-embed generation runs off the event loop."""
        if target_graph_id == graph_id:
            raise ValueError("cross-graph generation needs two distinct graphs")
        org = await self._owned_org(graph_id=graph_id, user_id=user_id)
        await self._owned_org(graph_id=target_graph_id, user_id=user_id)

        entities_a = await asyncio.to_thread(
            self._write.cross_graph_entities,
            graph_id=str(graph_id),
            organisation_id=org,
            limit=_CROSS_GRAPH_ENTITY_SCAN_LIMIT,
        )
        entities_b = await asyncio.to_thread(
            self._write.cross_graph_entities,
            graph_id=str(target_graph_id),
            organisation_id=org,
            limit=_CROSS_GRAPH_ENTITY_SCAN_LIMIT,
        )
        # The already-verdicted pairs (approved SAME_AS / rejected NOT_SAME_AS) — dropped before the
        # limit budget so they neither resurface nor over-count `generated`.
        verdicted = await asyncio.to_thread(
            self._write.verdicted_cross_graph_pairs,
            organisation_id=org,
            graph_id_a=str(graph_id),
            graph_id_b=str(target_graph_id),
        )
        embedder = make_embedder(self._settings)
        # The whole generation (quadratic cosine + the embedder's SYNC OpenAI HTTP) runs off the
        # event loop, like the entity fetches above — it must never block the KGS loop.
        candidates, warnings = await asyncio.to_thread(
            generate_cross_graph_pairs,
            graph_id_a=str(graph_id),
            entities_a=entities_a,
            graph_id_b=str(target_graph_id),
            entities_b=entities_b,
            candidate_threshold=candidate_threshold,
            embedder=embedder,
            limit=limit,
            skip_pairs=set(verdicted),
        )
        if candidates:
            await asyncio.to_thread(
                self._write.write_cross_graph_candidates,
                organisation_id=org,
                pairs=[
                    {
                        "id_a": c.node_id_a,
                        "graph_id_a": c.graph_id_a,
                        "id_b": c.node_id_b,
                        "graph_id_b": c.graph_id_b,
                        "score": c.score,
                        "method": c.method,
                    }
                    for c in candidates
                ],
            )
        return candidates, warnings

    @staticmethod
    def _canonical_pair(
        graph_id: uuid.UUID, other_graph_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Order the two graph ids deterministically so a cross-graph verdict keys the SAME audit
        row from either direction: the audit `graph_id` is always the smaller id, `other_graph_id`
        the larger. The candidate_id is already symmetric over the node-id pair, so the
        (smaller-graph, candidate_id) lookup is direction-independent: SAME_AS and NOT_SAME_AS
        cannot coexist for one pair, and a conflicting reverse verdict is a 409, not a 404."""
        if str(graph_id) <= str(other_graph_id):
            return graph_id, other_graph_id
        return other_graph_id, graph_id

    async def list_pending_cross_graph(
        self,
        *,
        graph_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int,
    ) -> list[CrossGraphCandidate]:
        """The pending cross-graph SAME_AS review queue touching this graph (#330) — what a HITL
        reviewer reads to see the candidates a prior generation run wrote (the queue is otherwise
        only returned in the generation response). Owner-gated (a graph not in the caller's
        org/owner → 404, no leak)."""
        org = await self._owned_org(graph_id=graph_id, user_id=user_id)
        rows = await asyncio.to_thread(
            self._write.pending_cross_graph_candidates,
            organisation_id=org,
            graph_id=str(graph_id),
            limit=limit,
        )
        return [
            CrossGraphCandidate(
                node_id_a=r["id_a"],
                node_id_b=r["id_b"],
                graph_id_a=r["graph_id_a"],
                graph_id_b=r["graph_id_b"],
                label=r["label"],
                name_a=r["name_a"],
                name_b=r["name_b"],
                score=r["score"],
                method=r["method"],
            )
            for r in rows
        ]

    async def _approve_cross_graph(
        self,
        *,
        graph_id: uuid.UUID,
        other_graph_id: uuid.UUID,
        user_id: uuid.UUID,
        pair: CandidatePair,
        candidate_id_path: str,
    ) -> LinkOutcome:
        """Approve a cross-graph candidate: LINK with `SAME_AS` (both graph ids stamped), drop the
        candidate edge, audit under the CANONICALISED pair (so a verdict from either direction
        resolves to one row). Idempotent on replay; 409 on a conflicting prior verdict from EITHER
        direction. A LINK is not a fold — the audit records both graph ids and no canonical
        survivor (both nodes survive in their own graphs)."""
        org = await self._owned_org(graph_id=graph_id, user_id=user_id)
        await self._owned_org(graph_id=other_graph_id, user_id=user_id)
        self._assert_candidate_id(pair, candidate_id_path)

        audit_graph, audit_other = self._canonical_pair(graph_id, other_graph_id)
        prior = await self._audit.find(graph_id=audit_graph, candidate_id=pair.candidate_id)
        if prior is not None:
            if prior.action != ResolutionAction.APPROVE.value:
                raise ResolutionConflict(pair.candidate_id)
            return LinkOutcome(
                node_id_a=pair.node_id_a,
                node_id_b=pair.node_id_b,
                graph_id_a=str(graph_id),
                graph_id_b=str(other_graph_id),
                linked=True,
            )

        endpoints = await asyncio.to_thread(
            self._write.candidate_endpoints_pair,
            organisation_id=org,
            graph_id_a=str(graph_id),
            node_id_a=pair.node_id_a,
            graph_id_b=str(other_graph_id),
            node_id_b=pair.node_id_b,
        )
        if endpoints is None:
            raise CandidateNotFound(pair.candidate_id)

        linked = await asyncio.to_thread(
            self._write.link_candidate,
            organisation_id=org,
            graph_id_a=str(graph_id),
            node_id_a=pair.node_id_a,
            graph_id_b=str(other_graph_id),
            node_id_b=pair.node_id_b,
        )
        # A cross-graph approve LINKS, never folds — record both graph ids and NO canonical survivor
        # (the in-graph approve sets canonical_node_id; a cross-graph LINK leaves both nodes alive).
        await self._audit.record(
            graph_id=audit_graph,
            other_graph_id=audit_other,
            candidate_id=pair.candidate_id,
            node_id_a=pair.node_id_a,
            node_id_b=pair.node_id_b,
            action=ResolutionAction.APPROVE,
            canonical_node_id=None,
            decided_by=user_id,
        )
        return LinkOutcome(
            node_id_a=pair.node_id_a,
            node_id_b=pair.node_id_b,
            graph_id_a=str(graph_id),
            graph_id_b=str(other_graph_id),
            linked=linked,
        )

    async def _reject_cross_graph(
        self,
        *,
        graph_id: uuid.UUID,
        other_graph_id: uuid.UUID,
        user_id: uuid.UUID,
        pair: CandidatePair,
        candidate_id_path: str,
    ) -> RejectOutcome:
        """Reject a cross-graph candidate: a both-ids `NOT_SAME_AS` suppression + drop the edge,
        audited under the CANONICALISED pair. Idempotent on replay; 409 on a conflicting prior
        verdict from EITHER direction."""
        org = await self._owned_org(graph_id=graph_id, user_id=user_id)
        await self._owned_org(graph_id=other_graph_id, user_id=user_id)
        self._assert_candidate_id(pair, candidate_id_path)

        audit_graph, audit_other = self._canonical_pair(graph_id, other_graph_id)
        prior = await self._audit.find(graph_id=audit_graph, candidate_id=pair.candidate_id)
        if prior is not None:
            if prior.action != ResolutionAction.REJECT.value:
                raise ResolutionConflict(pair.candidate_id)
            return RejectOutcome(
                node_id_a=pair.node_id_a, node_id_b=pair.node_id_b, suppressed=True
            )

        endpoints = await asyncio.to_thread(
            self._write.candidate_endpoints_pair,
            organisation_id=org,
            graph_id_a=str(graph_id),
            node_id_a=pair.node_id_a,
            graph_id_b=str(other_graph_id),
            node_id_b=pair.node_id_b,
        )
        if endpoints is None:
            raise CandidateNotFound(pair.candidate_id)

        suppressed = await asyncio.to_thread(
            self._write.suppress_candidate_pair,
            organisation_id=org,
            graph_id_a=str(graph_id),
            node_id_a=pair.node_id_a,
            graph_id_b=str(other_graph_id),
            node_id_b=pair.node_id_b,
        )
        await self._audit.record(
            graph_id=audit_graph,
            other_graph_id=audit_other,
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
