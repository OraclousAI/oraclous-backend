"""Agent-memory use-cases (ORAA-4 §21 services layer — all business logic lives here).

Issue #332 / ADR-027. Orchestrates the memory store/recall vertical:

* **store** — content-hash dedup → REAL embedding (the KGS embedder seam, fail-soft: an embed
  failure stores the memory without a vector and recall degrades to fulltext-only) → typed node
  create (org injected at the repository write boundary) → semantic contradiction detection
  (same subject+predicate, different object → CONTRADICTS, new wins, old invalidated) → entity
  linking (ABOUT).
* **search** — hybrid recall: fulltext + org+graph-scoped brute-force cosine + Ebbinghaus
  importance + recency (``domain/memory_decay.hybrid_rank``), lazily recomputed at read time and
  persisted by the access bump on every hit (NO decay cron).
* **context** — the token-budgeted "## Relevant Memory" markdown block (legacy assembly verbatim:
  Facts / Preferences / Recent activity), bumping access for the memories actually used.
* **supersede / delete** — temporal versioning (SUPERSEDES) and soft (valid_to) / hard
  (detach-delete) forget.
* **consolidate** — enqueues the per-(org,graph) similarity-consolidation job (tasks/).

Graph visibility gate: memories span user/team/organisation scopes (the legacy access level was
``viewer``), so the gate is ORG-scoped graph visibility (``GraphRepository.get`` returns None for
another org → 404), not the per-user owner gate — an org's agents and members share its memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from oraclous_knowledge_graph_service.domain.memory_decay import (
    base_importance_for,
    compute_importance,
    content_hash,
    hybrid_rank,
    recency_factor,
)
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.repositories.memory_repository import (
    MemoryDedupConflict,
    MemoryRepository,
)
from oraclous_knowledge_graph_service.schema.memory_schemas import (
    ConflictInfo,
    ContradictionResolution,
    MemoryContext,
    MemoryCreate,
    MemoryCreateResponse,
    MemorySearchResponse,
    MemorySearchResult,
    MemoryType,
    MemoryUpdate,
    MemoryUpdateResponse,
)
from oraclous_knowledge_graph_service.services.embedder import Embedder

logger = logging.getLogger(__name__)

# The lazily-created org-default memory graph (ADR-027 §5): where a harness run's memory lands when
# the run carries no graph context. ONE per org, identified by the reserved `system_kind` marker —
# never by its display name, so it can neither collide with nor resolve to a user-created graph that
# happens to share the name (the partial unique index makes find-or-create race-safe).
AGENT_MEMORY_SYSTEM_KIND = "agent_memory"
DEFAULT_MEMORY_GRAPH_NAME = "Agent Memory"
_DEFAULT_MEMORY_GRAPH_DESCRIPTION = (
    "Org-default agent memory graph (auto-created by the harness post-run memory hook)."
)

_TOKENS_PER_CHAR = 0.25  # ~4 chars per token (legacy heuristic)
_CANDIDATE_POOL = 50  # per-signal candidate fetch before ranking/limit


class MemoryNotFound(Exception):
    """The memory id has no CURRENT node in this org+graph. Maps to 404."""


class GraphNotVisible(Exception):
    """The graph does not exist in the caller's organisation. Maps to 404."""


def memory_consolidation_lock_key(*, organisation_id: str, graph_id: str) -> str:
    """The per-(org,graph) advisory lock key the consolidation job holds (#303/#305 pattern)."""
    return f"kgs:memory:consolidate:{organisation_id}:{graph_id}"


class MemoryService:
    """Memory store/recall use-cases over one bound (org, graph) repository scope."""

    def __init__(
        self,
        *,
        graphs: GraphRepository,
        repo_factory: Callable[[str], MemoryRepository],
        embedder: Embedder | None,
        enqueue_consolidation: Callable[[str, str], str],
        vector_candidate_cap: int = 1_000,
    ) -> None:
        self._graphs = graphs
        self._repo_factory = repo_factory
        self._embedder = embedder
        self._enqueue = enqueue_consolidation
        # Pre-cosine candidate cap passed to the repository's brute-force vector recall (#332 MED).
        self._vector_candidate_cap = vector_candidate_cap

    # ------------------------------------------------------------------ gates

    async def _visible_repo(self, graph_id: uuid.UUID) -> MemoryRepository:
        """Org-visibility gate: the graph must exist in the caller's org (else 404, no leak)."""
        graph = await self._graphs.get(graph_id)
        if graph is None:
            raise GraphNotVisible(str(graph_id))
        return self._repo_factory(str(graph_id))

    async def resolve_default_graph(self, *, user_id: uuid.UUID) -> uuid.UUID:
        """Find-or-create the org-default memory graph (lazy, org-scoped, race-safe).

        Used by the internal (harness) store path when a run carries no graph context. The graph is
        keyed on the reserved ``agent_memory`` system marker — NOT its display name — so it can
        never resolve to (or be shadowed by) a user-created graph called "Agent Memory", and the
        (org, system_kind) partial unique index makes concurrent first runs converge on one graph
        rather than creating duplicates (#332/ADR-027 §5)."""
        graph = await self._graphs.find_or_create_system_graph(
            user_id=user_id,
            system_kind=AGENT_MEMORY_SYSTEM_KIND,
            name=DEFAULT_MEMORY_GRAPH_NAME,
            description=_DEFAULT_MEMORY_GRAPH_DESCRIPTION,
        )
        return graph.id

    # ------------------------------------------------------------------ store

    def _embed(self, content: str) -> list[float] | None:
        """Fail-soft embedding: no embedder (no key) or an embed fault → None (fulltext-only)."""
        if self._embedder is None:
            return None
        try:
            return self._embedder.embed([content])[0]
        except Exception as exc:  # noqa: BLE001 — fail-soft: a vector is an enrichment, never a gate
            logger.warning("memory embedding failed (storing without a vector): %s", exc)
            return None

    @staticmethod
    def _extra_properties(req: MemoryCreate) -> dict[str, Any]:
        """The closed type-specific property set (legacy verbatim; bound params downstream)."""
        if req.type is MemoryType.EPISODIC:
            return {"event_type": req.event_type or "interaction", "user_id": req.user_id or ""}
        if req.type is MemoryType.SEMANTIC:
            return {
                "subject": req.subject or "",
                "predicate": req.predicate or "",
                "object": req.object or "",
                "is_negation": req.is_negation,
            }
        return {
            "category": req.category or "preference",
            "trigger_pattern": req.trigger_pattern or "",
            "times_applied": 0,
        }

    async def store(self, *, graph_id: uuid.UUID, req: MemoryCreate) -> MemoryCreateResponse:
        repo = await self._visible_repo(graph_id)
        now = datetime.now(UTC)
        chash = content_hash(req.content)

        # 1. Content-hash dedup (ADR-027 §1): an identical current memory of the SAME type+scope
        # returns, not re-stores (the type+scope keying stops a semantic deduping into an episodic).
        existing = await asyncio.to_thread(
            repo.find_by_content_hash, chash, memory_type=req.type.value, scope=req.scope.value
        )
        if existing:
            return MemoryCreateResponse(
                memory_id=existing["memory_id"],
                importance_score=float(existing["importance_score"]),
            )

        memory_id = str(uuid.uuid4())
        base_imp = base_importance_for(
            source=req.source.value, memory_type=req.type.value, confidence=req.confidence
        )
        embedding = await asyncio.to_thread(self._embed, req.content)

        try:
            await asyncio.to_thread(
                repo.create,
                memory_id=memory_id,
                memory_type=req.type.value,
                content=req.content,
                content_hash=chash,
                base_importance=base_imp,
                confidence=req.confidence,
                scope=req.scope.value,
                source=req.source.value,
                agent_id=req.agent_id or "",
                session_id=req.session_id or "",
                valid_from=req.valid_from or now,
                now=now,
                embedding=embedding,
                extra=self._extra_properties(req),
            )
        except MemoryDedupConflict:
            # Lost the dedup race (WP-11): a concurrent store of identical content won the
            # uniqueness constraint between our read-miss and this create. Treat as already-stored:
            # re-read the surviving current node by content hash and return it (idempotent, same as
            # the fast path above), never a 500.
            survivor = await asyncio.to_thread(
                repo.find_by_content_hash, chash, memory_type=req.type.value, scope=req.scope.value
            )
            if survivor is not None:
                return MemoryCreateResponse(
                    memory_id=survivor["memory_id"],
                    importance_score=float(survivor["importance_score"]),
                )
            raise  # the survivor vanished (superseded/deleted in the same instant) — surface it

        # 2. Contradiction detection (semantic only): new wins, old invalidated.
        conflicts: list[ConflictInfo] = []
        if req.type is MemoryType.SEMANTIC and req.subject and req.predicate:
            found = await asyncio.to_thread(
                repo.find_contradictions,
                memory_id=memory_id,
                subject=req.subject,
                predicate=req.predicate,
                object_=req.object or "",
                is_negation=req.is_negation,
            )
            for rec in found:
                await asyncio.to_thread(
                    repo.record_contradiction,
                    new_id=memory_id,
                    old_id=rec["memory_id"],
                    resolution=ContradictionResolution.NEW_WINS.value,
                    now=now,
                )
                conflicts.append(
                    ConflictInfo(
                        conflict_memory_id=rec["memory_id"],
                        content=rec["content"],
                        resolution=ContradictionResolution.NEW_WINS,
                    )
                )

        # 3. Entity linking (semantic): ABOUT edge to the matching graph entity.
        entity_linked: str | None = None
        if req.type is MemoryType.SEMANTIC and req.subject:
            entity_linked = await asyncio.to_thread(
                repo.link_to_entity, memory_id=memory_id, subject=req.subject
            )

        return MemoryCreateResponse(
            memory_id=memory_id,
            importance_score=base_imp,
            contradictions_detected=conflicts,
            entity_linked=entity_linked,
        )

    # ------------------------------------------------------------------ recall

    def _rank(
        self, candidates: dict[str, dict[str, Any]], *, hybrid: bool, now: datetime
    ) -> list[dict[str, Any]]:
        """Blend the per-candidate signals into the ranking score (domain math, lazily-decayed
        importance — the read side of the no-cron contract)."""
        max_text = max((c.get("text_score") or 0.0 for c in candidates.values()), default=0.0)
        ranked = []
        for c in candidates.values():
            text_norm = (c.get("text_score") or 0.0) / max_text if max_text > 0 else 0.0
            # The no-vector fallback (text carries the full retrieval weight) is a WHOLE-QUERY
            # property — it fires only when there is no query embedding at all (`hybrid` False).
            # When a query vector DOES exist, a candidate that simply had no vector hit (fulltext-
            # only, or below the vector cutoff) scores vector_score=0.0 — text*.25 + 0 — NOT a
            # promotion to text*.5 (#332 MED hybrid-rank fallback).
            vector_score = (c.get("vector_score") or 0.0) if hybrid else None
            importance = compute_importance(
                float(c["base_importance"]),
                str(c["memory_type"]),
                c["last_accessed_at"],
                int(c["access_count"]),
                now=now,
            )
            c["importance_now"] = importance
            c["relevance_score"] = hybrid_rank(
                text_score=text_norm,
                vector_score=vector_score,
                importance=importance,
                recency=recency_factor(c["last_accessed_at"], now=now),
            )
            ranked.append(c)
        ranked.sort(key=lambda c: (-c["relevance_score"], c["memory_id"]))
        return ranked

    async def search(
        self,
        *,
        graph_id: uuid.UUID,
        query: str,
        memory_type: MemoryType | None = None,
        scope: str | None = None,
        temporal: str = "current",
        min_confidence: float = 0.0,
        limit: int = 20,
    ) -> MemorySearchResponse:
        repo = await self._visible_repo(graph_id)
        now = datetime.now(UTC)
        filters: dict[str, Any] = {
            "memory_type": memory_type.value if memory_type else None,
            "scope": scope,
            "temporal": temporal,
            "min_confidence": min_confidence,
            "limit": _CANDIDATE_POOL,
        }

        candidates: dict[str, dict[str, Any]] = {}
        for row in await asyncio.to_thread(repo.fulltext_candidates, query=query, **filters):
            candidates[row["memory_id"]] = row

        qvec = await asyncio.to_thread(self._embed, query)
        if qvec is not None:
            for row in await asyncio.to_thread(
                repo.vector_candidates,
                query_vector=qvec,
                candidate_cap=self._vector_candidate_cap,
                **filters,
            ):
                merged = candidates.setdefault(row["memory_id"], row)
                merged["vector_score"] = row["vector_score"]

        ranked = self._rank(candidates, hybrid=qvec is not None, now=now)[:limit]

        # Access bump: every returned hit re-stamps last_accessed_at + persists the recomputed
        # importance (the lazy-decay write side).
        if ranked:
            await asyncio.to_thread(
                repo.bump_access, memory_ids=[c["memory_id"] for c in ranked], now=now
            )

        return MemorySearchResponse(
            memories=[self._to_result(c) for c in ranked], total=len(ranked)
        )

    @staticmethod
    def _to_result(c: dict[str, Any]) -> MemorySearchResult:
        return MemorySearchResult(
            memory_id=c["memory_id"],
            type=MemoryType(c["memory_type"]),
            content=c["content"],
            importance_score=float(c.get("importance_now", c.get("importance_score", 0.0))),
            relevance_score=float(c.get("relevance_score", 0.0)),
            confidence=float(c.get("confidence", 0.0)),
            valid_from=c.get("valid_from"),
            valid_to=c.get("valid_to"),
            scope=c.get("scope") or "agent",
            agent_id=c.get("agent_id") or None,
            session_id=c.get("session_id") or None,
            created_at=c.get("ingested_at"),
            last_accessed_at=c.get("last_accessed_at"),
            access_count=int(c.get("access_count", 0)),
        )

    async def context(
        self,
        *,
        graph_id: uuid.UUID,
        query: str,
        scopes: list[str] | None = None,
        max_tokens: int = 2000,
        include_types: list[str] | None = None,
    ) -> MemoryContext:
        """Token-budgeted '## Relevant Memory' block (legacy assembly verbatim), hybrid-ranked."""
        repo = await self._visible_repo(graph_id)
        t0 = time.monotonic()
        now = datetime.now(UTC)
        filters: dict[str, Any] = {
            "memory_type": None,
            "scope": None,
            "scopes": scopes,
            "include_types": include_types,
            "temporal": "current",
            "min_confidence": 0.0,
            "limit": _CANDIDATE_POOL,
        }
        candidates: dict[str, dict[str, Any]] = {}
        for row in await asyncio.to_thread(repo.fulltext_candidates, query=query, **filters):
            candidates[row["memory_id"]] = row
        qvec = await asyncio.to_thread(self._embed, query)
        if qvec is not None:
            for row in await asyncio.to_thread(
                repo.vector_candidates,
                query_vector=qvec,
                candidate_cap=self._vector_candidate_cap,
                **filters,
            ):
                merged = candidates.setdefault(row["memory_id"], row)
                merged["vector_score"] = row["vector_score"]
        ranked = self._rank(candidates, hybrid=qvec is not None, now=now)

        sections: dict[str, list[str]] = {"semantic": [], "procedural": [], "episodic": []}
        used_ids: list[str] = []
        estimated_tokens = 0
        for rec in ranked:
            entry = f"- {rec['content']} (confidence: {float(rec['confidence']):.2f})"
            entry_tokens = int(len(entry) * _TOKENS_PER_CHAR) + 5
            if estimated_tokens + entry_tokens > max_tokens:
                break
            sections.setdefault(str(rec["memory_type"]), []).append(entry)
            used_ids.append(rec["memory_id"])
            estimated_tokens += entry_tokens

        lines: list[str] = ["## Relevant Memory\n"]
        if sections.get("semantic"):
            lines.append("**Facts:**")
            lines.extend(sections["semantic"])
        if sections.get("procedural"):
            lines.append("\n**Preferences:**")
            lines.extend(sections["procedural"])
        if sections.get("episodic"):
            lines.append("\n**Recent activity:**")
            lines.extend(sections["episodic"])

        if used_ids:
            await asyncio.to_thread(repo.bump_access, memory_ids=used_ids, now=now)

        return MemoryContext(
            context_block="\n".join(lines),
            memories_used=used_ids,
            token_estimate=estimated_tokens,
            retrieval_ms=int((time.monotonic() - t0) * 1000),
        )

    # ------------------------------------------------------------ supersede / delete

    async def supersede(
        self, *, graph_id: uuid.UUID, memory_id: str, req: MemoryUpdate
    ) -> MemoryUpdateResponse:
        repo = await self._visible_repo(graph_id)
        now = datetime.now(UTC)
        old = await asyncio.to_thread(repo.get_current, memory_id)
        if old is None:
            raise MemoryNotFound(memory_id)

        new_content = req.content if req.content is not None else str(old["content"])
        new_confidence = req.confidence if req.confidence is not None else float(old["confidence"])
        content_changed = new_content != old["content"]
        embedding = await asyncio.to_thread(self._embed, new_content) if content_changed else None
        new_id = str(uuid.uuid4())
        base_imp = float(old.get("base_importance") or 0.8)

        ok = await asyncio.to_thread(
            repo.supersede,
            old_id=memory_id,
            new_id=new_id,
            memory_type=str(old["memory_type"]),
            content=new_content,
            content_hash=content_hash(new_content),
            scope=str(old["scope"]),
            confidence=new_confidence,
            base_importance=base_imp,
            embedding=embedding,
            content_changed=content_changed,
            reason=req.reason or "update",
            now=now,
        )
        if not ok:  # raced away between the read and the write
            raise MemoryNotFound(memory_id)
        return MemoryUpdateResponse(
            old_memory_id=memory_id, new_memory_id=new_id, superseded_at=now
        )

    async def delete(self, *, graph_id: uuid.UUID, memory_id: str, hard: bool = False) -> None:
        repo = await self._visible_repo(graph_id)
        if hard:
            found = await asyncio.to_thread(repo.hard_delete, memory_id=memory_id)
        else:
            found = await asyncio.to_thread(
                repo.soft_delete, memory_id=memory_id, now=datetime.now(UTC)
            )
        if not found:
            raise MemoryNotFound(memory_id)

    # ------------------------------------------------------------ consolidation

    async def consolidate(self, *, graph_id: uuid.UUID) -> str:
        """Gate the graph, then enqueue the per-(org,graph) similarity-consolidation job."""
        await self._visible_repo(graph_id)
        from oraclous_substrate.access import enforced_organisation_id

        return self._enqueue(str(graph_id), enforced_organisation_id())
