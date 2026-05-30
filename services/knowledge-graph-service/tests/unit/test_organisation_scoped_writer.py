"""Outer organisation-scoping layer for the multi-tenant KG writer
(ORA-18 / Epic A3, the *Extend* step — writer half).

These tests pin the new contract for ``OrganisationScopedKGWriter``: scope by
``organisation_id`` (taken from the resolved ``OrganisationContext`` — never
from a request body) before applying the legacy ``graph_id`` stamp.
``organisation_id`` is outermost; ``graph_id`` is inner; the existing legacy
writer behaviour covered in [test_multi_tenant_writer.py](./test_multi_tenant_writer.py)
is preserved.

Threats mitigated: T1 (cross-tenant data leakage). The fail-closed
construction pattern aligns with ADR-006 (organisation_id on every operation)
and the [A2] enforcement story (ORA-17).

Module placement: ``solution-architect`` ratified Option B (split) on
31 May 2026 — the writer lives in ``knowledge-graph-service`` (write path);
the three retriever wrappers' organisation-scoping tests live in
``services/knowledge-retriever-service/tests/unit/test_organisation_scoping_layer.py``.
Both consume the substrate seam ``oraclous_substrate.access`` per
[ADR-012](https://oraclous.atlassian.net/wiki/spaces/OP/pages/2490396) §1;
neither forks org-scoping.

Imports of the not-yet-built seams ``oraclous_governance.context`` (ORA-14)
and ``oraclous_knowledge_graph_service.multi_tenant`` (ORA-18 impl) are
function-local per ORA-48 / TST001 (the TDD-window collection-safety
convention): collection succeeds, each test fails RED at *runtime* with
``ModuleNotFoundError`` until the paired ``[impl]`` PRs land.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Local-import helpers
# ---------------------------------------------------------------------------


_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SEED_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PRINCIPAL = uuid.UUID("99999999-9999-9999-9999-999999999999")


def _context(organisation_id: uuid.UUID = _ORG_A):
    """Build an ``OrganisationContext``; imports the seam locally."""
    from oraclous_governance.context import OrganisationContext, PrincipalType

    return OrganisationContext(
        organisation_id=organisation_id,
        principal_id=_PRINCIPAL,
        principal_type=PrincipalType.USER,
    )


def _writer_cls():
    """Return ``OrganisationScopedKGWriter``; imports the seam locally."""
    from oraclous_knowledge_graph_service.multi_tenant import (
        OrganisationScopedKGWriter,
    )

    return OrganisationScopedKGWriter


# ---------------------------------------------------------------------------
# Test doubles
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
    def __init__(self) -> None:
        self.runs: list[_Graph] = []
        self.driver = object()
        self.neo4j_database = "neo4j"

    async def run(self, graph: _Graph) -> None:
        self.runs.append(graph)


# ---------------------------------------------------------------------------
# Construction: organisation context is REQUIRED and fail-closed
# ---------------------------------------------------------------------------


class TestWriterConstructionFailClosed:
    """The writer cannot be constructed without an authenticated
    ``OrganisationContext`` — there is no implicit / default scope.

    Threat: T1. Aligns with ADR-006 and the [A2] enforcement contract
    (ORA-17): ``organisation_id`` comes from authenticated context only,
    never from a request body or a default.
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    def test_writer_requires_organisation_context(self) -> None:
        Writer = _writer_cls()
        with pytest.raises((TypeError, ValueError)):
            Writer(
                base_writer=_CapturingBaseWriter(),
                context=None,  # type: ignore[arg-type]
                graph_id="graph-A",
            )


# ---------------------------------------------------------------------------
# Writer: organisation_id injected, unconditionally overwritten
# ---------------------------------------------------------------------------


class TestWriterOrganisationScopeInjection:
    """The outer writer layer stamps ``organisation_id`` on every node and
    relationship, unconditionally overriding any caller-supplied value (T1).

    Mirrors the existing ``graph_id`` security contract proven in
    [test_multi_tenant_writer.py::TestGraphIdOverwriteSecurity]."""

    async def test_writer_injects_organisation_id_on_every_node(self) -> None:
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(id="n1", label="Person", properties={"name": "Alice"}),
                _Node(id="n2", label="Person", properties={"name": "Bob"}),
            ]
        )

        await writer.run(graph)

        assert all(n.properties["organisation_id"] == str(_ORG_A) for n in graph.nodes)

    async def test_writer_injects_organisation_id_on_every_relationship(self) -> None:
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(id="a", label="Person", properties={"name": "Alice"}),
                _Node(id="b", label="Person", properties={"name": "Bob"}),
            ],
            relationships=[
                _Rel(type="KNOWS", start_node_id="a", end_node_id="b"),
            ],
        )

        await writer.run(graph)

        assert graph.relationships[0].properties["organisation_id"] == str(_ORG_A)

    @pytest.mark.security
    async def test_writer_unconditionally_overwrites_caller_supplied_organisation_id_on_node(
        self,
    ) -> None:
        """T1: a node whose properties include a different ``organisation_id``
        MUST NOT be written to that other organisation."""
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(
                    id="n1",
                    label="Person",
                    properties={"name": "Alice", "organisation_id": str(_ORG_B)},
                )
            ]
        )

        await writer.run(graph)

        assert graph.nodes[0].properties["organisation_id"] == str(_ORG_A)

    @pytest.mark.security
    async def test_writer_unconditionally_overwrites_caller_organisation_id_on_rel(
        self,
    ) -> None:
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(
            nodes=[
                _Node(id="a", label="Person", properties={"name": "Alice"}),
                _Node(id="b", label="Person", properties={"name": "Bob"}),
            ],
            relationships=[
                _Rel(
                    type="KNOWS",
                    start_node_id="a",
                    end_node_id="b",
                    properties={"organisation_id": str(_ORG_B)},
                )
            ],
        )

        await writer.run(graph)

        assert graph.relationships[0].properties["organisation_id"] == str(_ORG_A)

    async def test_writer_preserves_legacy_graph_id_alongside_organisation_id(
        self,
    ) -> None:
        """``organisation_id`` is added *alongside* ``graph_id`` — the inner
        ``graph_id`` boundary survives unchanged (Lift step preserved)."""
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_ORG_A), graph_id="graph-A")
        graph = _Graph(nodes=[_Node(id="n1", label="Person", properties={"name": "Alice"})])

        await writer.run(graph)

        props = graph.nodes[0].properties
        assert props["organisation_id"] == str(_ORG_A)
        assert props["graph_id"] == "graph-A"


# ---------------------------------------------------------------------------
# Single-org regression: seed-org context preserves legacy behaviour
# ---------------------------------------------------------------------------


class TestWriterSingleOrgRegression:
    """Acceptance criterion: "Single-organisation behaviour unchanged".

    With the seed organisation, the writer behaves identically to the legacy
    single-graph_id flow — ``organisation_id`` is injected but the
    graph_id-only behaviour matches what existed before.
    """

    async def test_writer_in_seed_org_writes_nodes_unchanged_except_for_org_id(
        self,
    ) -> None:
        """Single-org regression: the only new property is ``organisation_id``;
        all legacy properties (``graph_id``, timestamps, ``created_by``) are
        identical to the lift-only flow."""
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        writer = Writer(base_writer=base, context=_context(_SEED_ORG), graph_id="graph-A")
        graph = _Graph(nodes=[_Node(id="n1", label="Person", properties={"name": "Alice"})])

        await writer.run(graph)

        props = graph.nodes[0].properties
        assert props["organisation_id"] == str(_SEED_ORG)
        assert props["graph_id"] == "graph-A"
        assert "transaction_time" in props
        assert "ingestion_time" in props
        assert props["created_by"] == "multi_tenant_pipeline"
