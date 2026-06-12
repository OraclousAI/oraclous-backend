"""ResolutionService use-case logic (#279) — approve/reject, idempotency, conflict, owner gate.

In isolation from HTTP/Neo4j/Postgres: the owner gate is the REAL GraphService over a fake metadata
repo; the Neo4j mutation + the audit log are fakes that record calls. Proves the governance-relevant
behaviour the route relies on: a non-owner is invisible (404), a replayed verdict does not mutate
twice, a conflicting second reviewer is rejected, and a path/body candidate-id mismatch is a 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.domain.resolution import (
    CandidateNotFound,
    CandidatePair,
    ResolutionAction,
    ResolutionConflict,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound, GraphService
from oraclous_knowledge_graph_service.services.resolution_service import ResolutionService

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_OWNER = uuid.uuid4()
_INTRUDER = uuid.uuid4()
_GRAPH_ID = uuid.uuid4()
_A = "node-a"
_B = "node-b"


class _FakeGraphRepo:
    def __init__(self, owner: uuid.UUID) -> None:
        now = datetime(2026, 6, 12, tzinfo=UTC)
        self._graph = Graph(
            id=_GRAPH_ID,
            organisation_id=_ORG,
            user_id=owner,
            name="g",
            description=None,
            status="active",
            node_count=0,
            relationship_count=0,
            created_at=now,
            updated_at=now,
        )

    async def get(self, graph_id: uuid.UUID) -> Graph | None:
        return self._graph if graph_id == self._graph.id else None


class _FakeWriteRepo:
    """Records Neo4j mutations; `candidate_present` toggles whether the pair is still pending."""

    def __init__(self, candidate_present: bool = True) -> None:
        self.candidate_present = candidate_present
        self.merged: list[tuple[str, str]] = []
        self.suppressed: list[tuple[str, str]] = []

    def candidate_endpoints(self, *, graph_id, organisation_id, node_id_a, node_id_b):
        if not self.candidate_present:
            return None
        return {
            "id_a": node_id_a,
            "id_b": node_id_b,
            "labels_a": ["Company"],
            "labels_b": ["Company"],
            "aliases_a": ["Eurail"],
            "aliases_b": ["Eurail B.V."],
            "name_a": "Eurail",
            "name_b": "Eurail B.V.",
        }

    def merge_candidate(self, *, graph_id, organisation_id, survivor_id, merged_id):
        self.merged.append((survivor_id, merged_id))
        self.candidate_present = False  # edge resolved away
        return {
            "survivor_id": survivor_id,
            "repointed_edges": 2,
            "aliases": ["Eurail", "Eurail B.V."],
        }

    def suppress_candidate(self, *, graph_id, organisation_id, node_id_a, node_id_b):
        self.suppressed.append((node_id_a, node_id_b))
        self.candidate_present = False
        return True


class _FakeAuditRepo:
    """In-memory entity_resolutions audit log keyed by (graph_id, candidate_id)."""

    class _Row:
        def __init__(self, action: str, canonical_node_id: str | None, decided_by: uuid.UUID):
            self.action = action
            self.canonical_node_id = canonical_node_id
            self.decided_by = decided_by

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str], _FakeAuditRepo._Row] = {}

    async def find(self, *, graph_id, candidate_id):
        return self.rows.get((graph_id, candidate_id))

    async def record(
        self, *, graph_id, candidate_id, node_id_a, node_id_b, action, canonical_node_id, decided_by
    ):
        row = self._Row(action.value, canonical_node_id, decided_by)
        self.rows[(graph_id, candidate_id)] = row
        return row


def _service(
    owner: uuid.UUID, write_repo: _FakeWriteRepo, audit: _FakeAuditRepo
) -> ResolutionService:
    return ResolutionService(
        graph_service=GraphService(_FakeGraphRepo(owner)),
        write_repo=write_repo,
        audit_repo=audit,
    )


def _pair() -> CandidatePair:
    return CandidatePair(node_id_a=_A, node_id_b=_B)


async def test_approve_merges_and_audits() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(_OWNER, write, audit)
    pair = _pair()
    outcome = await svc.approve(
        graph_id=_GRAPH_ID, user_id=_OWNER, pair=pair, candidate_id_path=pair.candidate_id
    )
    assert outcome.survivor_id == _A and outcome.merged_id == _B
    assert outcome.repointed_edges == 2
    assert write.merged == [(_A, _B)]
    row = audit.rows[(_GRAPH_ID, pair.candidate_id)]
    assert row.action == ResolutionAction.APPROVE.value
    assert row.canonical_node_id == _A and row.decided_by == _OWNER


async def test_reject_suppresses_and_audits() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(_OWNER, write, audit)
    pair = _pair()
    outcome = await svc.reject(
        graph_id=_GRAPH_ID, user_id=_OWNER, pair=pair, candidate_id_path=pair.candidate_id
    )
    assert outcome.suppressed is True
    assert write.suppressed == [(_A, _B)]
    assert audit.rows[(_GRAPH_ID, pair.candidate_id)].action == ResolutionAction.REJECT.value


async def test_approve_replay_is_idempotent_no_second_merge() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(_OWNER, write, audit)
    pair = _pair()
    await svc.approve(
        graph_id=_GRAPH_ID, user_id=_OWNER, pair=pair, candidate_id_path=pair.candidate_id
    )
    # A second identical approve must NOT mutate the graph again (the edge is already gone).
    again = await svc.approve(
        graph_id=_GRAPH_ID, user_id=_OWNER, pair=pair, candidate_id_path=pair.candidate_id
    )
    assert again.survivor_id == _A
    assert write.merged == [(_A, _B)]  # merge fired exactly once


async def test_conflicting_verdict_is_rejected() -> None:
    # The pair was approved; a later REJECT of the SAME pair (a second reviewer who also owns the
    # graph, or the owner changing their mind) → 409 conflict, no second mutation. The owner gate is
    # a separate concern (covered by test_non_owner_cannot_resolve).
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(_OWNER, write, audit)
    pair = _pair()
    await svc.approve(
        graph_id=_GRAPH_ID, user_id=_OWNER, pair=pair, candidate_id_path=pair.candidate_id
    )
    with pytest.raises(ResolutionConflict):
        await svc.reject(
            graph_id=_GRAPH_ID, user_id=_OWNER, pair=pair, candidate_id_path=pair.candidate_id
        )
    assert write.suppressed == []


async def test_approve_unknown_candidate_is_not_found() -> None:
    write = _FakeWriteRepo(candidate_present=False)
    svc = _service(_OWNER, write, _FakeAuditRepo())
    pair = _pair()
    with pytest.raises(CandidateNotFound):
        await svc.approve(
            graph_id=_GRAPH_ID, user_id=_OWNER, pair=pair, candidate_id_path=pair.candidate_id
        )
    assert write.merged == []


async def test_non_owner_cannot_resolve() -> None:
    write = _FakeWriteRepo()
    svc = _service(_OWNER, write, _FakeAuditRepo())  # graph owned by _OWNER
    pair = _pair()
    with pytest.raises(GraphNotFound):
        await svc.approve(
            graph_id=_GRAPH_ID, user_id=_INTRUDER, pair=pair, candidate_id_path=pair.candidate_id
        )
    assert write.merged == []  # the owner gate runs before any Neo4j access


async def test_path_candidate_id_must_match_body_pair() -> None:
    # A path candidate-id that is not the canonical hash of the body's node pair is a 404 — the URL
    # identity and the operands cannot disagree (no acting on a different pair than the URL names).
    write = _FakeWriteRepo()
    svc = _service(_OWNER, write, _FakeAuditRepo())
    with pytest.raises(CandidateNotFound):
        await svc.approve(
            graph_id=_GRAPH_ID,
            user_id=_OWNER,
            pair=_pair(),
            candidate_id_path="deadbeef-not-the-hash",
        )
    assert write.merged == []
