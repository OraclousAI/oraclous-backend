"""Two gates, not one: the org-scoped READ gate and the owner WRITE gate (#736, ADR-051).

Today `GraphService.get_graph` (via `_owned_or_404`) is a single owner check serving twelve call
sites that mix reads and writes. ADR-051 decision 2 says the listing gate must equal the read gate;
decision 3 says mutation stays owner-scoped. One check cannot do both, so this file pins the shape
the implementation must take:

* a READ gate — org-scoped, no owner filter — behind every pure read: the graph list, graph detail,
  documents, artifacts (list and content), ingest-job status, the ontology read, analytics reads;
* the existing WRITE gate — `_owned_or_404` — untouched, behind create/rename/delete, ingest submit,
  the ontology write, resolution decisions and the cross-org grant.

Both halves are asserted everywhere: a same-org non-owner gets in on reads, and a caller in another
organisation gets `GraphNotFound` on every one of them. The negative is what proves ADR-051 removed
no control. The org boundary itself is enforced in the repository (`enforced_organisation_id`), so
the fake repo below models it the way the real one behaves: a row outside the bound org is simply
not visible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oraclous_knowledge_graph_service.domain.graph import Graph
from oraclous_knowledge_graph_service.services.graph_service import GraphNotFound, GraphService

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-0000000005ff")
_OWNER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000000a2")  # same org, invited, owns nothing
_OUTSIDER = uuid.UUID("00000000-0000-0000-0000-0000000000b9")  # a different organisation


def _graph(*, user_id: uuid.UUID, name: str, organisation_id: uuid.UUID = _ORG) -> Graph:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    return Graph(
        id=uuid.uuid4(),
        organisation_id=organisation_id,
        user_id=user_id,
        name=name,
        description=None,
        status="active",
        node_count=0,
        relationship_count=0,
        created_at=now,
        updated_at=now,
    )


class _FakeRepo:
    """In-memory GraphRepository. `bound_org` stands in for `enforced_organisation_id()`: every
    read is filtered by it, so a graph in another organisation is invisible rather than forbidden —
    exactly what the real repository does."""

    def __init__(self, bound_org: uuid.UUID = _ORG) -> None:
        self.bound_org = bound_org
        self.rows: dict[uuid.UUID, Graph] = {}

    def seed(self, graph: Graph) -> Graph:
        self.rows[graph.id] = graph
        return graph

    def _visible(self) -> list[Graph]:
        return [g for g in self.rows.values() if g.organisation_id == self.bound_org]

    async def create(self, *, user_id, name, description, system_kind=None) -> Graph:
        return self.seed(_graph(user_id=user_id, name=name, organisation_id=self.bound_org))

    async def list_for_user(self, *, user_id) -> list[Graph]:
        return [g for g in self._visible() if g.user_id == user_id]

    async def list_for_org(self) -> list[Graph]:
        return self._visible()

    async def get(self, graph_id) -> Graph | None:
        g = self.rows.get(graph_id)
        return g if g is not None and g.organisation_id == self.bound_org else None

    async def update(
        self, graph_id, *, name, description, model_credential_id=None
    ) -> Graph | None:
        return await self.get(graph_id)

    async def delete(self, graph_id) -> bool:
        return self.rows.pop(graph_id, None) is not None


@pytest.fixture
def repo() -> _FakeRepo:
    return _FakeRepo()


@pytest.fixture
def owners_graph(repo: _FakeRepo) -> Graph:
    """The admin's workspace — the company knowledge base every member is meant to find."""
    return repo.seed(_graph(user_id=_OWNER, name="Company Knowledge Base"))


# ── the READ gate: org-scoped, no owner filter ────────────────────────────────────────────────


async def test_a_member_reads_a_graph_they_do_not_own(repo, owners_graph) -> None:
    """ADR-051 decision 2. The member can already read this graph's content through the org-scoped
    `POST /v1/search/*`; the read gate stops pretending otherwise."""
    svc = GraphService(repo)
    got = await svc.get_readable_graph(graph_id=owners_graph.id)
    assert got.id == owners_graph.id
    assert got.user_id == _OWNER  # ownership is reported, not required


async def test_the_read_gate_still_refuses_another_organisation(repo, owners_graph) -> None:
    """The negative half. An outsider's request binds THEIR org, so the row is invisible."""
    outsider_repo = _FakeRepo(bound_org=_OTHER_ORG)
    outsider_repo.rows = repo.rows  # the same stored graphs, a different bound organisation
    svc = GraphService(outsider_repo)
    with pytest.raises(GraphNotFound):
        await svc.get_readable_graph(graph_id=owners_graph.id)


async def test_assert_readable_is_the_gate_only_form(repo, owners_graph) -> None:
    """The read paths that need a gate but not the graph (documents, artifacts, analytics) get a
    void-returning form, so no caller pays for the live-count overlay it does not use."""
    svc = GraphService(repo)
    assert await svc.assert_readable(graph_id=owners_graph.id) is None
    with pytest.raises(GraphNotFound):
        await svc.assert_readable(graph_id=uuid.uuid4())


async def test_a_missing_graph_is_not_found_through_the_read_gate(repo) -> None:
    svc = GraphService(repo)
    with pytest.raises(GraphNotFound):
        await svc.get_readable_graph(graph_id=uuid.uuid4())


# ── the WRITE gate: unchanged, owner-scoped ───────────────────────────────────────────────────


async def test_the_owner_gate_still_refuses_a_same_org_member(repo, owners_graph) -> None:
    """ADR-051 decision 3. `get_graph` remains the OWNER gate — relaxing it in place would have
    silently widened ingest, the ontology write and resolution decisions, which all call it."""
    svc = GraphService(repo)
    with pytest.raises(GraphNotFound):
        await svc.get_graph(graph_id=owners_graph.id, user_id=_MEMBER)


async def test_a_member_cannot_rename_a_graph_they_can_read(repo, owners_graph) -> None:
    svc = GraphService(repo)
    assert (await svc.get_readable_graph(graph_id=owners_graph.id)).id == owners_graph.id
    with pytest.raises(GraphNotFound):
        await svc.update_graph(
            graph_id=owners_graph.id, user_id=_MEMBER, name="hijacked", description=None
        )
    assert repo.rows[owners_graph.id].name == "Company Knowledge Base"


async def test_a_member_cannot_delete_a_graph_they_can_read(repo, owners_graph) -> None:
    svc = GraphService(repo)
    with pytest.raises(GraphNotFound):
        await svc.delete_graph(graph_id=owners_graph.id, user_id=_MEMBER)
    assert owners_graph.id in repo.rows


async def test_the_public_owner_gate_is_unchanged(repo, owners_graph) -> None:
    """`assert_owned` backs the cross-org grant (#446): only an owner may share a graph."""
    svc = GraphService(repo)
    assert await svc.assert_owned(graph_id=owners_graph.id, user_id=_OWNER) is None
    with pytest.raises(GraphNotFound):
        await svc.assert_owned(graph_id=owners_graph.id, user_id=_MEMBER)


# ── the list: the same set the read gate admits, and the same set federation aggregates ───────


async def test_the_list_returns_the_organisations_graphs(repo, owners_graph) -> None:
    """Acceptance 1. The member's own graph and the admin's both appear."""
    svc = GraphService(repo)
    mine = repo.seed(_graph(user_id=_MEMBER, name="My Notes"))
    names = {g.name for g in await svc.list_graphs()}
    assert names == {"Company Knowledge Base", "My Notes"}
    assert {g.id for g in await svc.list_graphs()} == {owners_graph.id, mine.id}


async def test_the_list_excludes_another_organisation(repo, owners_graph) -> None:
    """Acceptance 4. A graph in another org never appears, however many members the caller's org
    has. This is the control ADR-051 must not remove."""
    repo.seed(_graph(user_id=_OUTSIDER, name="Rival Corp KB", organisation_id=_OTHER_ORG))
    svc = GraphService(repo)
    listed = await svc.list_graphs()
    assert {g.id for g in listed} == {owners_graph.id}
    assert "Rival Corp KB" not in {g.name for g in listed}


async def test_the_list_is_exactly_the_federation_set(repo, owners_graph) -> None:
    """Acceptance 5, proved rather than asserted: both surfaces go through `list_for_org`, so the
    set a user sees in the console is the set ADR-026 federated search fans out over — never one
    graph more, never one fewer."""
    repo.seed(_graph(user_id=_MEMBER, name="My Notes"))
    repo.seed(_graph(user_id=_OUTSIDER, name="Rival Corp KB", organisation_id=_OTHER_ORG))
    svc = GraphService(repo)
    assert {g.id for g in await svc.list_graphs()} == {g.id for g in await svc.list_org_graphs()}


async def test_every_listed_graph_passes_the_read_gate(repo, owners_graph) -> None:
    """The listing gate EQUALS the read gate (decision 2): nothing is listed that then 404s, and the
    console never renders a row it cannot open."""
    repo.seed(_graph(user_id=_MEMBER, name="My Notes"))
    svc = GraphService(repo)
    for listed in await svc.list_graphs():
        assert (await svc.get_readable_graph(graph_id=listed.id)).id == listed.id
