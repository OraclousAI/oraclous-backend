"""Converge the multi-tenant KG writer onto the substrate
org-scoping helper + authenticated-context binding (Code Review follow-up).

This module pins the two follow-up acceptance criteria on the writer side:

1. **AC1 — Single source of truth (ADR-012 §1/§1b).**
   ``OrganisationScopedKGWriter._injected_properties`` stamps the property
   key from the canonical substrate constant
   ``oraclous_substrate.access.ORGANISATION_ID_PROPERTY`` rather than
   embedding the string literal ``"organisation_id"`` inline.

2. **AC2 — Authenticated-context binding (T1-M1; security-architect
   required).** The writer's effective ``organisation_id`` stamp comes from
   the bound governance context (``current_organisation_context()``), never
   from a constructor argument or a request body. A spoofed constructor
   argument cannot redirect the stamp.

(AC3 in the brief targets the retriever's ``_keep_item`` — it has no writer
analogue. See
``services/knowledge-retriever-service/tests/unit/test_ora52_substrate_convergence.py``.)

Imports of substrate symbols that may not yet exist (``ORGANISATION_ID_PROPERTY``)
are function-local per TST001 so collection succeeds; tests fail at
runtime with ``ImportError`` until the paired ``[impl]`` PR lands.

Threats: T1 (cross-tenant writes via property injection /
constructor-arg spoofing).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    use_organisation_context,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Identifiers + test doubles
# ---------------------------------------------------------------------------


_ORG_AUTHENTIC = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_SPOOFED = uuid.UUID("22222222-2222-2222-2222-222222222222")
_PRINCIPAL = uuid.UUID("99999999-9999-9999-9999-999999999999")


def _context(org: uuid.UUID = _ORG_AUTHENTIC) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=org,
        principal_id=_PRINCIPAL,
        principal_type=PrincipalType.USER,
    )


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

    async def run(self, graph: _Graph) -> None:
        self.runs.append(graph)


def _writer_cls():
    from oraclous_knowledge_graph_service.multi_tenant import (
        OrganisationScopedKGWriter,
    )

    return OrganisationScopedKGWriter


# ---------------------------------------------------------------------------
# AC1 — Composition of the substrate property-name helper
# ---------------------------------------------------------------------------


class TestWriterComposesSubstrateHelper:
    """``OrganisationScopedKGWriter._injected_properties`` MUST stamp the
    organisation property key via the canonical substrate constant
    ``ORGANISATION_ID_PROPERTY`` — not the inlined literal. Verified by
    patching the constant to a sentinel and observing the sentinel as the
    property key on a written node.
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    async def test_writer_stamps_via_canonical_property_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from oraclous_substrate import access

        sentinel_property = "SENTINEL_organisation_id"
        monkeypatch.setattr(access, "ORGANISATION_ID_PROPERTY", sentinel_property)

        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            writer = Writer(
                base_writer=base,
                context=_context(_ORG_AUTHENTIC),
                graph_id="graph-A",
            )
            graph = _Graph(
                nodes=[
                    _Node(id="n1", label="Person", properties={"name": "Alice"}),
                ]
            )
            await writer.run(graph)

        props = graph.nodes[0].properties or {}
        assert sentinel_property in props, (
            "AC1: _injected_properties must key the organisation stamp via "
            "ORGANISATION_ID_PROPERTY. The sentinel-renamed constant did not "
            "propagate — the writer is inlining 'organisation_id' directly."
        )
        assert props[sentinel_property] == str(_ORG_AUTHENTIC)


# ---------------------------------------------------------------------------
# AC2 — Authenticated-context binding (request body cannot redirect stamp)
# ---------------------------------------------------------------------------


class TestWriterAuthenticatedContextBinding:
    """The writer's effective ``organisation_id`` stamp MUST come from the
    bound ``current_organisation_context()`` — never from a constructor
    argument or a request body.

    Two complementary properties pin this:

    * Running the writer without a bound context fails closed (no implicit /
      default scope).
    * A construction-time argument purporting to set a different organisation
      cannot redirect the stamp: either the constructor refuses the kwarg
      (``TypeError``), or the writer ignores its value and stamps the bound
      context at runtime.
    """

    pytestmark = [pytest.mark.unit, pytest.mark.security]

    async def test_writer_fails_closed_when_no_context_bound_at_run(self) -> None:
        """If the bound context disappears between construction and ``.run()``
        (e.g. a request handler dropped it), the writer must refuse to stamp
        rather than silently fall back to a constructor-arg value (T1-M1)."""
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        # Construct under the authentic bound context.
        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            writer = Writer(
                base_writer=base,
                context=_context(_ORG_AUTHENTIC),
                graph_id="graph-A",
            )

        graph = _Graph(nodes=[_Node(id="n1", label="Person", properties={"name": "Alice"})])
        # No bound context at run time — must refuse.
        with pytest.raises((MissingOrganisationContextError, ValueError, RuntimeError)):
            await writer.run(graph)

    async def test_writer_stamp_tracks_bound_context_not_construction_arg(
        self,
    ) -> None:
        """``_injected_properties`` resolves the ``organisation_id`` value
        from the bound context at *runtime*. Construction under context A,
        running under context A → stamp is A — the seam is the source, not
        a captured arg."""
        Writer = _writer_cls()
        base = _CapturingBaseWriter()
        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            writer = Writer(
                base_writer=base,
                context=_context(_ORG_AUTHENTIC),
                graph_id="graph-A",
            )
            graph = _Graph(nodes=[_Node(id="n1", label="Person", properties={"name": "Alice"})])
            await writer.run(graph)

        props = graph.nodes[0].properties or {}
        assert props.get("organisation_id") == str(_ORG_AUTHENTIC), (
            "AC2: writer stamp must equal the BOUND context's "
            "organisation_id, not a captured construction-time arg."
        )

    async def test_construction_arg_cannot_redirect_writer_stamp(self) -> None:
        """Spoofing attempt: bind authentic org A, supply spoofed org B as a
        construction-time context argument. The writer's *runtime* stamp must
        still be A — never B. Acceptable implementations:

        * Drop the ``context=`` kwarg entirely (``TypeError`` at construction).
        * Keep the kwarg but ignore it; runtime reads the bound context.
        * Keep the kwarg but raise on mismatch with the bound context.

        Any of the three satisfies ``stamp == bound``.
        """
        Writer = _writer_cls()
        base = _CapturingBaseWriter()

        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            try:
                writer = Writer(
                    base_writer=base,
                    context=_context(_ORG_SPOOFED),  # the spoof attempt
                    graph_id="graph-A",
                )
            except (TypeError, ValueError):
                # Construction refused the spoof — invariant satisfied. STOP.
                return

            graph = _Graph(
                nodes=[
                    _Node(id="n1", label="Person", properties={"name": "Alice"}),
                ],
                relationships=[],
            )
            await writer.run(graph)

        props = graph.nodes[0].properties or {}
        stamped = props.get("organisation_id")
        assert stamped == str(_ORG_AUTHENTIC), (
            "AC2: a constructor argument MUST NOT redirect the writer stamp. "
            f"Stamp was {stamped!r} but bound context was {_ORG_AUTHENTIC!s}."
        )
        assert stamped != str(_ORG_SPOOFED), (
            "AC2: the spoofed construction-time organisation_id leaked into "
            "the writer's stamp — T1-M1 violation."
        )

    async def test_construction_arg_cannot_redirect_relationship_stamp(
        self,
    ) -> None:
        """Mirror of the node check on relationships — the writer's
        relationship stamp must also follow the bound context, not the
        spoofed construction arg.
        """
        Writer = _writer_cls()
        base = _CapturingBaseWriter()

        with use_organisation_context(_context(_ORG_AUTHENTIC)):
            try:
                writer = Writer(
                    base_writer=base,
                    context=_context(_ORG_SPOOFED),
                    graph_id="graph-A",
                )
            except (TypeError, ValueError):
                return

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

        rel_props = graph.relationships[0].properties or {}
        stamped = rel_props.get("organisation_id")
        assert stamped == str(_ORG_AUTHENTIC), (
            "AC2: relationship stamp must equal the BOUND context's "
            f"organisation_id; got {stamped!r}."
        )
