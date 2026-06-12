"""ResolutionService cross-graph use-cases (#330) — generate, link-on-approve, owner gates.

In isolation from HTTP/Neo4j/Postgres, mirroring the in-graph service test: the owner gate is the
REAL GraphService over a fake two-graph metadata repo; the Neo4j mutations + audit are fakes. The
governance-relevant behaviour: BOTH graphs are owner-gated (a graph the caller does not own —
including any other org's graph, invisible by org-scoping — is a 404, so a cross-ORG pair is
impossible); an approve LINKS (never folds); replay is idempotent; a conflicting verdict is 409;
generation refuses a self-pair and writes candidates carrying both graph ids.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.domain.resolution import (
    CandidateNotFound,
    CandidatePair,
    LinkOutcome,
    ResolutionAction,
    ResolutionConflict,
)
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound, GraphService
from oraclous_knowledge_graph_service.services.resolution_service import ResolutionService

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_OWNER = uuid.uuid4()
_INTRUDER = uuid.uuid4()
_GRAPH_A = uuid.uuid4()
_GRAPH_B = uuid.uuid4()
_FOREIGN_GRAPH = uuid.uuid4()  # another org's graph: invisible to the org-scoped repo
_A = "node-a"
_B = "node-b"


class _FakeGraphRepo:
    """Two org-owned graphs; anything else (e.g. another org's graph) is invisible (None)."""

    def __init__(self, owner: uuid.UUID) -> None:
        now = datetime(2026, 6, 12, tzinfo=UTC)
        self._graphs = {
            gid: Graph(
                id=gid,
                organisation_id=_ORG,
                user_id=owner,
                name=f"g-{gid}",
                description=None,
                status="active",
                node_count=0,
                relationship_count=0,
                created_at=now,
                updated_at=now,
            )
            for gid in (_GRAPH_A, _GRAPH_B)
        }

    async def get(self, graph_id: uuid.UUID) -> Graph | None:
        return self._graphs.get(graph_id)


class _FakeWriteRepo:
    """Records the cross-graph Neo4j mutations; `candidate_present` toggles the pending edge."""

    def __init__(self, candidate_present: bool = True) -> None:
        self.candidate_present = candidate_present
        self.linked: list[tuple[str, str, str, str]] = []
        self.suppressed: list[tuple[str, str, str, str]] = []
        self.written_pairs: list[dict] = []
        self.entities = {
            str(_GRAPH_A): [
                {"id": _A, "name": "acme corp", "canonical_name": "Acme Corp", "label": "Company"}
            ],
            str(_GRAPH_B): [
                {"id": _B, "name": "acme corp", "canonical_name": "Acme Corp", "label": "Company"}
            ],
        }

    def cross_graph_entities(self, *, graph_id, organisation_id, limit):
        assert organisation_id == str(_ORG)
        return list(self.entities.get(graph_id, []))[:limit]

    def write_cross_graph_candidates(self, *, organisation_id, pairs):
        assert organisation_id == str(_ORG)
        self.written_pairs.extend(pairs)
        return len(pairs)

    def candidate_endpoints_pair(
        self, *, organisation_id, graph_id_a, node_id_a, graph_id_b, node_id_b
    ):
        if not self.candidate_present:
            return None
        return {"id_a": node_id_a, "id_b": node_id_b, "name_a": "Acme Corp", "name_b": "Acme Corp"}

    def link_candidate(self, *, organisation_id, graph_id_a, node_id_a, graph_id_b, node_id_b):
        self.linked.append((graph_id_a, node_id_a, graph_id_b, node_id_b))
        self.candidate_present = False
        return True

    def suppress_candidate_pair(
        self, *, organisation_id, graph_id_a, node_id_a, graph_id_b, node_id_b
    ):
        self.suppressed.append((graph_id_a, node_id_a, graph_id_b, node_id_b))
        self.candidate_present = False
        return True


class _FakeAuditRepo:
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


def _service(write: _FakeWriteRepo, audit: _FakeAuditRepo) -> ResolutionService:
    # hashing embedder (key-free) — the same default the in-graph resolution pass uses.
    return ResolutionService(
        graph_service=GraphService(_FakeGraphRepo(_OWNER)),
        write_repo=write,
        audit_repo=audit,
        settings=Settings(),
    )


def _pair() -> CandidatePair:
    return CandidatePair(node_id_a=_A, node_id_b=_B)


# ── generation ───────────────────────────────────────────────────────────────────────────────


async def test_generate_writes_candidates_carrying_both_graph_ids() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(write, audit)
    candidates, warnings = await svc.generate_cross_graph(
        graph_id=_GRAPH_A,
        target_graph_id=_GRAPH_B,
        user_id=_OWNER,
        candidate_threshold=0.85,
        limit=100,
    )
    assert warnings == []
    assert len(candidates) == 1
    assert candidates[0].method == "canonical_key"
    assert write.written_pairs == [
        {
            "id_a": _A,
            "graph_id_a": str(_GRAPH_A),
            "id_b": _B,
            "graph_id_b": str(_GRAPH_B),
            "score": 1.0,
            "method": "canonical_key",
        }
    ]


async def test_generate_refuses_a_self_pair() -> None:
    svc = _service(_FakeWriteRepo(), _FakeAuditRepo())
    with pytest.raises(ValueError, match="distinct graphs"):
        await svc.generate_cross_graph(
            graph_id=_GRAPH_A,
            target_graph_id=_GRAPH_A,
            user_id=_OWNER,
            candidate_threshold=0.85,
            limit=100,
        )


async def test_generate_against_an_invisible_graph_is_404() -> None:
    # _FOREIGN_GRAPH is not in the org-scoped repo (another org / unknown) → GraphNotFound;
    # a cross-ORG candidate pair can therefore never be generated.
    write = _FakeWriteRepo()
    svc = _service(write, _FakeAuditRepo())
    with pytest.raises(GraphNotFound):
        await svc.generate_cross_graph(
            graph_id=_GRAPH_A,
            target_graph_id=_FOREIGN_GRAPH,
            user_id=_OWNER,
            candidate_threshold=0.85,
            limit=100,
        )
    assert write.written_pairs == []


async def test_generate_by_a_non_owner_is_404() -> None:
    svc = _service(_FakeWriteRepo(), _FakeAuditRepo())
    with pytest.raises(GraphNotFound):
        await svc.generate_cross_graph(
            graph_id=_GRAPH_A,
            target_graph_id=_GRAPH_B,
            user_id=_INTRUDER,
            candidate_threshold=0.85,
            limit=100,
        )


# ── verdicts ─────────────────────────────────────────────────────────────────────────────────


async def test_cross_graph_approve_links_and_audits() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(write, audit)
    pair = _pair()
    outcome = await svc.approve(
        graph_id=_GRAPH_A,
        user_id=_OWNER,
        pair=pair,
        candidate_id_path=pair.candidate_id,
        other_graph_id=_GRAPH_B,
    )
    assert isinstance(outcome, LinkOutcome) and outcome.linked is True
    assert write.linked == [(str(_GRAPH_A), _A, str(_GRAPH_B), _B)]
    row = audit.rows[(_GRAPH_A, pair.candidate_id)]
    assert row.action == ResolutionAction.APPROVE.value and row.canonical_node_id == _A


async def test_cross_graph_approve_replay_is_idempotent() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(write, audit)
    pair = _pair()
    kwargs = dict(
        graph_id=_GRAPH_A,
        user_id=_OWNER,
        pair=pair,
        candidate_id_path=pair.candidate_id,
        other_graph_id=_GRAPH_B,
    )
    await svc.approve(**kwargs)
    again = await svc.approve(**kwargs)
    assert isinstance(again, LinkOutcome) and again.linked is True
    assert write.linked == [(str(_GRAPH_A), _A, str(_GRAPH_B), _B)]  # linked exactly once


async def test_cross_graph_conflicting_verdict_is_409() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(write, audit)
    pair = _pair()
    await svc.approve(
        graph_id=_GRAPH_A,
        user_id=_OWNER,
        pair=pair,
        candidate_id_path=pair.candidate_id,
        other_graph_id=_GRAPH_B,
    )
    with pytest.raises(ResolutionConflict):
        await svc.reject(
            graph_id=_GRAPH_A,
            user_id=_OWNER,
            pair=pair,
            candidate_id_path=pair.candidate_id,
            other_graph_id=_GRAPH_B,
        )
    assert write.suppressed == []


async def test_cross_graph_reject_suppresses_with_both_ids() -> None:
    write, audit = _FakeWriteRepo(), _FakeAuditRepo()
    svc = _service(write, audit)
    pair = _pair()
    outcome = await svc.reject(
        graph_id=_GRAPH_A,
        user_id=_OWNER,
        pair=pair,
        candidate_id_path=pair.candidate_id,
        other_graph_id=_GRAPH_B,
    )
    assert outcome.suppressed is True
    assert write.suppressed == [(str(_GRAPH_A), _A, str(_GRAPH_B), _B)]
    assert audit.rows[(_GRAPH_A, pair.candidate_id)].action == ResolutionAction.REJECT.value


async def test_cross_graph_verdict_on_an_invisible_graph_is_404() -> None:
    write = _FakeWriteRepo()
    svc = _service(write, _FakeAuditRepo())
    pair = _pair()
    with pytest.raises(GraphNotFound):
        await svc.approve(
            graph_id=_GRAPH_A,
            user_id=_OWNER,
            pair=pair,
            candidate_id_path=pair.candidate_id,
            other_graph_id=_FOREIGN_GRAPH,
        )
    assert write.linked == []


async def test_cross_graph_unknown_candidate_is_404() -> None:
    write = _FakeWriteRepo(candidate_present=False)
    svc = _service(write, _FakeAuditRepo())
    pair = _pair()
    with pytest.raises(CandidateNotFound):
        await svc.approve(
            graph_id=_GRAPH_A,
            user_id=_OWNER,
            pair=pair,
            candidate_id_path=pair.candidate_id,
            other_graph_id=_GRAPH_B,
        )
    assert write.linked == []
