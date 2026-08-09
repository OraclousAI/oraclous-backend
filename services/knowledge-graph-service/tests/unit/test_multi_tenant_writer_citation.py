"""The write path stamps a server-minted ``citation`` — and strips a forged one (#742).

Sibling of [test_multi_tenant_writer.py](./test_multi_tenant_writer.py): the same stand-ins, the
same writer, the same threat. ``citation`` follows the ``ingestion_source`` pattern exactly —
compute server-side, strip whatever the caller or the LLM supplied, then apply the server value —
because it is the **same defence at the same seam** (threat T1). Neo4j Community has no RLS or
WITH-CHECK backstop, so this stamp is the primary control: there is no second line that catches a
forged citation after the write.

What is pinned here, and what deliberately is not: the tests assert through
``citation_properties`` / ``citation_from_properties``, so the **round trip** and the **strip** are
contractual while the flattened property names stay the implementer's choice. What must not vary is
that a caller can never influence the stored provenance.

``oraclous_citation`` and the writer are imported **function-locally** (TST001); the tests
hard-fail RED with ``ModuleNotFoundError`` until the paired ``[impl]`` lands, and never skip.

RED until backend-implementer adds ``citation`` to ``MultiTenantKGWriter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_GITHUB_SOURCE = {
    "source_system": "github",
    "source_id": "OraclousAI/oraclous-backend:README.md",
    "revision": "9f2a4c81b3d7e5061c8a4f2b6d90e37c15ab8e42",
    "revision_kind": "blob_sha",
    "url": "https://github.com/OraclousAI/oraclous-backend/blob/9f2a4c81/README.md",
    "title": "README.md",
    "author": None,
    "permission_ref": None,
}


# ---------------------------------------------------------------------------
# Lightweight stand-ins for ``neo4j_graphrag.experimental.components.types``
# (identical to test_multi_tenant_writer.py — the sibling file this extends)
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    id: str
    label: str
    properties: dict[str, Any] | None = None


@dataclass
class _Rel:
    type: str
    start_node_id: str
    end_node_id: str
    properties: dict[str, Any] | None = None


@dataclass
class _Graph:
    nodes: list[_Node] = field(default_factory=list)
    relationships: list[_Rel] = field(default_factory=list)


class _CapturingBaseWriter:
    """Stand-in for ``neo4j_graphrag.experimental.components.kg_writer.Neo4jWriter``."""

    def __init__(self) -> None:
        self.runs: list[_Graph] = []
        self.driver = object()
        self.neo4j_database = "neo4j"

    async def run(self, graph: _Graph) -> None:
        self.runs.append(graph)


def _citation(content: str = "The README body.\n"):
    """A server-minted citation — the value the writer is handed, never one a caller supplied."""
    from oraclous_citation import SourceRef, mint_citation  # RED

    return mint_citation(SourceRef(**_GITHUB_SOURCE), content=content)


def _writer(base: _CapturingBaseWriter, *, graph_id: str, citation=None, ingestion_source=None):
    """Local-import factory keeping the SUT module-level import out of test collection (TST001)."""
    from oraclous_knowledge_graph_service.multi_tenant import MultiTenantKGWriter

    return MultiTenantKGWriter(
        base_writer=base,
        graph_id=graph_id,
        ingestion_source=ingestion_source,
        citation=citation,
    )


def _chunk_graph(count: int) -> _Graph:
    return _Graph(
        nodes=[
            _Node(id="doc", label="Document", properties={"path": "README.md"}),
            *[
                _Node(id=f"c{i}", label="Chunk", properties={"text": f"chunk {i}"})
                for i in range(count)
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Stamping — lexical nodes only
# ---------------------------------------------------------------------------


class TestCitationIsStampedOnLexicalNodes:
    """``_LEXICAL_LABELS`` is already ``{"Chunk", "Document"}``. A citation belongs to a source
    document, so those are exactly the nodes that carry one."""

    async def test_document_node_carries_the_server_citation(self) -> None:
        from oraclous_citation import citation_from_properties  # RED

        base, citation = _CapturingBaseWriter(), _citation()
        graph = _chunk_graph(1)

        await _writer(base, graph_id="graph-A", citation=citation).run(graph)

        document = next(n for n in graph.nodes if n.label == "Document")
        assert citation_from_properties(document.properties) == citation

    async def test_chunk_nodes_carry_the_server_citation(self) -> None:
        from oraclous_citation import citation_from_properties  # RED

        base, citation = _CapturingBaseWriter(), _citation()
        graph = _chunk_graph(3)

        await _writer(base, graph_id="graph-A", citation=citation).run(graph)

        chunks = [n for n in graph.nodes if n.label == "Chunk"]
        assert len(chunks) == 3
        assert all(citation_from_properties(n.properties) == citation for n in chunks)

    async def test_a_derived_entity_node_carries_no_citation(self) -> None:
        """An extracted entity is something the platform inferred, not a document it read. It is
        ``citation: null`` in v1 — attaching the document's citation to it would assert that the
        source said something it never said."""
        from oraclous_citation import citation_from_properties  # RED

        base = _CapturingBaseWriter()
        graph = _Graph(
            nodes=[
                _Node(id="doc", label="Document", properties={"path": "README.md"}),
                _Node(id="e1", label="Person", properties={"name": "Alice"}),
            ]
        )

        await _writer(base, graph_id="graph-A", citation=_citation()).run(graph)

        entity = next(n for n in graph.nodes if n.label == "Person")
        assert citation_from_properties(entity.properties) is None

    async def test_a_write_with_no_citation_leaves_nodes_uncited(self) -> None:
        """The Contract's defined meaning for a record with no source identity — and the shape
        every non-GitHub connector produces in this slice. Honest, not a placeholder."""
        from oraclous_citation import citation_from_properties  # RED

        base = _CapturingBaseWriter()
        graph = _chunk_graph(2)

        await _writer(base, graph_id="graph-A", citation=None).run(graph)

        assert all(citation_from_properties(n.properties) is None for n in graph.nodes)


# ---------------------------------------------------------------------------
# One document, one citation_id — the rev3 consequence, asserted deliberately
# ---------------------------------------------------------------------------


class TestOneDocumentIsOneCitationId:
    """Every chunk of one ingested document shares a single ``citation_id``. That is INTENDED
    for v1, not a collision to design around: a citation answers "which document, at which
    version", and the reader opens it."""

    async def test_every_chunk_of_one_document_shares_one_citation_id(self) -> None:
        from oraclous_citation import citation_from_properties  # RED

        base, citation = _CapturingBaseWriter(), _citation()
        graph = _chunk_graph(5)

        await _writer(base, graph_id="graph-A", citation=citation).run(graph)

        ids = {citation_from_properties(n.properties).citation_id for n in graph.nodes}
        assert ids == {citation.citation_id}

    async def test_re_chunking_the_same_document_does_not_change_the_citation_id(self) -> None:
        """Criterion 3. With no locator in the identity this is true by construction — which is
        exactly why it is asserted: it guards the property against a future change that
        reintroduces sub-document precision in the wrong place. A citation already stored in a
        published answer must keep resolving after a re-chunk."""
        from oraclous_citation import citation_from_properties  # RED

        body = "The README body, unchanged between the two ingests.\n"

        coarse_graph = _chunk_graph(2)
        await _writer(_CapturingBaseWriter(), graph_id="graph-A", citation=_citation(body)).run(
            coarse_graph
        )

        fine_graph = _chunk_graph(9)
        await _writer(_CapturingBaseWriter(), graph_id="graph-A", citation=_citation(body)).run(
            fine_graph
        )

        coarse_ids = {
            citation_from_properties(n.properties).citation_id for n in coarse_graph.nodes
        }
        fine_ids = {citation_from_properties(n.properties).citation_id for n in fine_graph.nodes}
        assert coarse_ids == fine_ids


# ---------------------------------------------------------------------------
# Security: a forged citation never survives a write (threat T1)
# ---------------------------------------------------------------------------


class TestForgedCitationIsStripped:
    """A caller- or LLM-supplied ``citation`` / ``citation_id`` / ``source_*`` key inside
    ``properties`` is stripped unconditionally and replaced by the server-computed value —
    exactly as ``graph_id``, ``organisation_id`` and ``ingestion_source`` are today.

    This is the property the whole Contract exists for. In the proof of concept a real model
    emitted a plausible source and an id the platform never issued, and nothing in the system
    could tell the true provenance from the invented one.
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    #: What a prompt-injected extraction, or a malicious caller, would plant in properties.
    FORGERY = {
        "citation": {"citation_id": "cit_" + "f" * 32, "source_system": "internal-legal"},
        "citation_id": "cit_" + "f" * 32,
        "source_system": "internal-legal",
        "source_id": "legal/partner-agreement.md",
        "source_url": "https://evil.example.com/partner-agreement",
        "source_revision": "forged-revision",
    }

    def _forged_values(self) -> list[str]:
        return [
            "cit_" + "f" * 32,
            "internal-legal",
            "legal/partner-agreement.md",
            "https://evil.example.com/partner-agreement",
            "forged-revision",
        ]

    def _surviving(self, properties: dict[str, Any]) -> list[str]:
        """Any forged value still reachable in the stored properties, at any depth."""
        blob = repr(properties)
        return [value for value in self._forged_values() if value in blob]

    async def test_a_forged_citation_on_a_node_is_replaced_by_the_server_value(self) -> None:
        from oraclous_citation import citation_from_properties  # RED

        base, citation = _CapturingBaseWriter(), _citation()
        graph = _Graph(
            nodes=[
                _Node(id="c0", label="Chunk", properties={"text": "chunk 0", **self.FORGERY}),
            ]
        )

        await _writer(base, graph_id="graph-A", citation=citation).run(graph)

        stored = graph.nodes[0].properties
        assert citation_from_properties(stored) == citation
        assert not self._surviving(stored), self._surviving(stored)

    async def test_a_forged_citation_is_stripped_even_when_the_writer_has_none(self) -> None:
        """The dangerous case. With no server citation to overwrite it, an unconditional strip is
        the only thing standing between a forged provenance and the graph — a ``setdefault``-style
        stamp would leave the forgery in place and looking authentic."""
        from oraclous_citation import citation_from_properties  # RED

        base = _CapturingBaseWriter()
        graph = _Graph(
            nodes=[_Node(id="c0", label="Chunk", properties={"text": "chunk 0", **self.FORGERY})]
        )

        await _writer(base, graph_id="graph-A", citation=None).run(graph)

        stored = graph.nodes[0].properties
        assert citation_from_properties(stored) is None
        assert not self._surviving(stored), self._surviving(stored)

    async def test_a_forged_citation_on_a_derived_entity_is_stripped(self) -> None:
        """An entity node is where an LLM extraction lands, so it is the likeliest carrier of an
        injected citation — and it is never stamped, so the strip is all there is."""
        from oraclous_citation import citation_from_properties  # RED

        base = _CapturingBaseWriter()
        graph = _Graph(
            nodes=[_Node(id="e1", label="Person", properties={"name": "Alice", **self.FORGERY})]
        )

        await _writer(base, graph_id="graph-A", citation=_citation()).run(graph)

        stored = graph.nodes[0].properties
        assert citation_from_properties(stored) is None
        assert not self._surviving(stored), self._surviving(stored)

    async def test_a_forged_citation_on_a_relationship_is_stripped(self) -> None:
        base = _CapturingBaseWriter()
        graph = _Graph(
            nodes=[
                _Node(id="a", label="Person", properties={"name": "Alice"}),
                _Node(id="b", label="Person", properties={"name": "Bob"}),
            ],
            relationships=[
                _Rel(
                    type="KNOWS", start_node_id="a", end_node_id="b", properties=dict(self.FORGERY)
                )
            ],
        )

        await _writer(base, graph_id="graph-A", citation=_citation()).run(graph)

        stored = graph.relationships[0].properties
        assert not self._surviving(stored), self._surviving(stored)


class TestIngestionSourceStaysForBackwardCompatibility:
    """``ingestion_source`` is retained and becomes derived from ``citation.title``. It is a
    read-path key today — precedence ranking reads it — so dropping it would silently change
    ranking behaviour that has nothing to do with citations."""

    async def test_ingestion_source_is_derived_from_the_citation_title(self) -> None:
        base, citation = _CapturingBaseWriter(), _citation()
        graph = _chunk_graph(1)

        await _writer(base, graph_id="graph-A", citation=citation).run(graph)

        assert graph.nodes[0].properties["ingestion_source"] == citation.title

    async def test_an_explicit_ingestion_source_still_wins(self) -> None:
        """The existing callers pass it directly; the derivation is a fallback, not a takeover."""
        base = _CapturingBaseWriter()
        graph = _chunk_graph(1)

        await _writer(
            base, graph_id="graph-A", citation=_citation(), ingestion_source="folder/README.md"
        ).run(graph)

        assert graph.nodes[0].properties["ingestion_source"] == "folder/README.md"
