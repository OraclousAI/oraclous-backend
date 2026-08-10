"""Multi-tenant write-path wrappers for the knowledge graph service.

Lifts ``MultiTenantKGWriter`` from the legacy
``knowledge-graph-builder/app/components/multi_tenant_components.py`` and extends
it with an outer organisation-scoping layer (``OrganisationScopedKGWriter``).

Module placement: ``solution-architect`` ratified Option B (split) on
31 May 2026 — the writer lives here in ``knowledge-graph-service`` (write
path); the three retriever wrappers live in
``oraclous_knowledge_retriever_service.multi_tenant`` (read path). Both
consume the substrate seam ``oraclous_substrate.access`` per ADR-012 §1;
neither forks org-scoping.

Threats: T1 (cross-tenant leakage via property injection). The
``organisation_id`` stamp is unconditional — a caller / LLM-extracted property
that pins a different organisation's id is overwritten so a write can never
land in another tenant's scope. Neo4j community has no RLS / WITH-CHECK
backstop (see ADR-012 §1 implementation note), so this stamp is the primary
write-isolation control. The ``citation`` stamp is the same defence at the same
seam: a caller- or LLM-supplied citation is stripped unconditionally before the
server-minted value is applied, so a forged provenance never reaches the graph.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Module-level neo4j-graphrag imports so tests can monkeypatch ``Neo4jWriter``.
from neo4j_graphrag.experimental.components.kg_writer import Neo4jWriter
from oraclous_citation import Citation, citation_properties
from oraclous_citation.graph_properties import is_citation_property

# Module-level (NOT a ``from-import``) so the substrate's canonical
# ``ORGANISATION_ID_PROPERTY`` is reached via attribute lookup at use-time —
# this is what makes it a single source of truth (ADR-012 §1b).
from oraclous_substrate import access

if TYPE_CHECKING:
    from oraclous_governance import OrganisationContext

logger = logging.getLogger(__name__)

_MAX_INGESTION_SOURCE_LEN = 512
# Labels that legitimately have no ``name`` property (chunks/documents carry
# ``text`` / ``path`` instead). Empty-name entity filtering only applies to
# other labels — i.e. things the LLM classified as entity types.
_LEXICAL_LABELS = frozenset({"Chunk", "Document"})
# A caller/LLM property claiming where content came from. Stripped alongside the citation keys —
# the connector is the only party that may state a source, and it does so out of band.
_CLAIMED_SOURCE_PREFIX = "source_"


def _strip_claimed_provenance(properties: dict[str, object]) -> None:
    """Remove every caller- or LLM-supplied citation-shaped key, in place.

    Unconditional, and deliberately broader than the key set the writer itself stamps: a bare
    ``citation``, anything under the ``citation_`` prefix, and any ``source_*`` key are all
    provenance a caller was never issued. Stripping only when the writer HAS a citation to
    overwrite would leave a forgery in place on an uncited write, looking authentic — which is the
    dangerous case, since nothing downstream can tell a planted citation from a real one.
    """
    for key in [
        key
        for key in properties
        if is_citation_property(key) or key.startswith(_CLAIMED_SOURCE_PREFIX)
    ]:
        del properties[key]


def _sanitize_source(raw: str | None) -> str | None:
    """Strip null bytes + leading/trailing whitespace; enforce max length.

    Returns ``None`` when the cleaned result is empty or the input was ``None``.
    """
    if raw is None:
        return None
    cleaned = raw.replace("\x00", "").strip()[:_MAX_INGESTION_SOURCE_LEN]
    return cleaned or None


class MultiTenantKGWriter:
    """Multi-tenant wrapper around ``Neo4jWriter`` — preserves the legacy
    ``graph_id`` injection contract (Lift step).

    Automatically stamps ``graph_id`` + bitemporal ``transaction_time`` /
    ``ingestion_time`` on every node and relationship. Drops empty-name
    entities (TASK-061) with their dangling relationships. Collapses identical
    ``(src, type, tgt)`` relationships into one weighted edge (TASK-062).
    """

    def __init__(
        self,
        base_writer: Neo4jWriter,
        graph_id: str,
        user_id: str | None = None,
        ingestion_source: str | None = None,
        citation: Citation | None = None,
    ) -> None:
        self.base_writer = base_writer
        self.graph_id = graph_id
        self.user_id = user_id
        self.ingestion_source = ingestion_source
        # An ALREADY-MINTED citation. The writer stamps, it never mints: §CITE mints once at the
        # tool-execution boundary, through a function general over a SourceRef, and the writer
        # sits well below that boundary (it holds chunks, not the document content). Exactly the
        # relationship it already has with ``ingestion_source``.
        self.citation = citation

    def _injected_properties(self, now: datetime) -> dict[str, object]:
        """Properties stamped on every node and relationship in one ``run``.

        Subclasses override to add their own scope (e.g.
        ``OrganisationScopedKGWriter`` adds ``organisation_id``).
        """
        return {
            "graph_id": self.graph_id,
            "created_by": "multi_tenant_pipeline",
            "transaction_time": now,
            "ingestion_time": now,
        }

    async def run(self, graph: object) -> None:
        """Write the graph with automatic tenant + provenance stamping."""
        now = datetime.now(UTC)

        # Drop empty-name entities (TASK-061). Track ids so we can also drop
        # any relationship that would dangle on a removed node.
        dropped_ids: set[str] = set()
        kept_nodes = []
        for node in graph.nodes:  # type: ignore[attr-defined]
            label = getattr(node, "label", None)
            if label in _LEXICAL_LABELS:
                kept_nodes.append(node)
                continue
            raw_name = (node.properties or {}).get("name") if node.properties else None
            if not (isinstance(raw_name, str) and raw_name.strip()):
                dropped_ids.add(node.id)
                continue
            kept_nodes.append(node)

        if dropped_ids:
            logger.info(
                "MultiTenantKGWriter: dropped %d empty-name entities for tenant %s, source=%s",
                len(dropped_ids),
                self.graph_id,
                self.ingestion_source,
            )
            graph.nodes = kept_nodes  # type: ignore[attr-defined]
            kept_rels = [
                r
                for r in graph.relationships  # type: ignore[attr-defined]
                if r.start_node_id not in dropped_ids and r.end_node_id not in dropped_ids
            ]
            dropped_rel_count = len(graph.relationships) - len(kept_rels)  # type: ignore[attr-defined]
            if dropped_rel_count:
                logger.info(
                    "MultiTenantKGWriter: dropped %d relationships pointing to filtered nodes",
                    dropped_rel_count,
                )
                graph.relationships = kept_rels  # type: ignore[attr-defined]

        injected = self._injected_properties(now)
        # An explicit ingestion_source still WINS; deriving from the citation title is a fallback,
        # not a takeover. ``GraphWriteRepository._delete_document`` matches on the exact value
        # passed in to get replace-on-re-ingest, and the retriever's precedence ranking reads the
        # same field as a path — an unconditional override would silently stop replacing and start
        # accumulating duplicates.
        safe_source = _sanitize_source(self.ingestion_source) or _sanitize_source(
            self.citation.title if self.citation else None
        )
        stamped_citation = citation_properties(self.citation) if self.citation else None

        for node in graph.nodes:  # type: ignore[attr-defined]
            if not node.properties:
                node.properties = {}
            # graph_id (and organisation_id in the subclass) is unconditional —
            # caller / LLM properties cannot redirect a write to another tenant.
            node.properties.update(injected)
            # Always strip any caller/LLM-provided ingestion_source and apply
            # the sanitised writer value (prompt-injection defence).
            node.properties.pop("ingestion_source", None)
            if safe_source is not None:
                node.properties["ingestion_source"] = safe_source
            _strip_claimed_provenance(node.properties)
            # Lexical nodes only. A derived or extracted entity is something the platform
            # inferred, not a document it read — attaching the document's citation to it would
            # assert that the source said something it never said, so it stays uncited in v1.
            if stamped_citation is not None and getattr(node, "label", None) in _LEXICAL_LABELS:
                node.properties.update(stamped_citation)
            if self.user_id:
                node.properties["user_id"] = self.user_id

        # Collapse identical (src, type, tgt) relationships into one weighted
        # edge (TASK-062). Done before stamping so all stamps land on the
        # collapsed primary.
        if graph.relationships:  # type: ignore[attr-defined]
            rel_groups: dict[tuple[str, str, str], list[object]] = {}
            for rel in graph.relationships:  # type: ignore[attr-defined]
                key = (rel.start_node_id, rel.type, rel.end_node_id)
                rel_groups.setdefault(key, []).append(rel)
            collapsed = 0
            deduped: list[object] = []
            for rels in rel_groups.values():
                primary = rels[0]
                if not primary.properties:  # type: ignore[attr-defined]
                    primary.properties = {}  # type: ignore[attr-defined]
                primary.properties["weight"] = len(rels)  # type: ignore[attr-defined]
                deduped.append(primary)
                collapsed += len(rels) - 1
            if collapsed:
                logger.info(
                    "MultiTenantKGWriter: collapsed %d duplicate relationships "
                    "into %d unique edges",
                    collapsed,
                    len(deduped),
                )
            graph.relationships = deduped  # type: ignore[attr-defined]

        for rel in graph.relationships:  # type: ignore[attr-defined]
            if not rel.properties:
                rel.properties = {}
            rel.properties.update(injected)
            rel.properties.pop("ingestion_source", None)
            if safe_source is not None:
                rel.properties["ingestion_source"] = safe_source
            # Stripped but never stamped: a citation belongs to a source document, and an edge is
            # not one. The strip still applies — a relationship is an LLM-extraction landing site.
            _strip_claimed_provenance(rel.properties)
            if self.user_id:
                rel.properties["user_id"] = self.user_id

        logger.info(
            "Writing graph with %d nodes and %d relationships for tenant %s",
            len(graph.nodes),  # type: ignore[attr-defined]
            len(graph.relationships),  # type: ignore[attr-defined]
            self.graph_id,
        )

        # The wrapper's contract is "write the graph" (-> None); the base writer's return model is
        # unused by the sole caller (graph_write_repository), so await the write and return None.
        await self.base_writer.run(graph)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        """Delegate other attributes to the wrapped base writer."""
        return getattr(self.base_writer, name)


class OrganisationScopedKGWriter(MultiTenantKGWriter):
    """Outer organisation-scoping wrapper above ``MultiTenantKGWriter``
    (the Extend step).

    Stamps ``organisation_id`` on every node and relationship in addition to
    ``graph_id``. The stamp value is sourced live from the substrate seam
    via ``access.enforced_organisation_id`` at run time — never from a
    constructor argument or a request body (ADR-012 §1b / T1-M1 / AC2).
    ``organisation_id`` is outermost; ``graph_id`` is inner; a caller /
    LLM-extracted ``organisation_id`` on a node or relationship is
    unconditionally overwritten with the bound-context value (T1 defence).

    The property key is sourced from the substrate's canonical
    ``access.ORGANISATION_ID_PROPERTY`` (AC1 — single source of truth)
    so a rename of the property name propagates from one site.

    The ``context`` keyword argument is accepted for backward compatibility
    with the original call sites but is **deliberately ignored** —
    the substrate seam is the single source of truth.

    The signature is keyword-only and enumerates its arguments explicitly, so
    ``citation`` has to be forwarded here as well: this is the class the real
    write path constructs, and adding the argument to the base alone would
    leave that path unable to carry a citation at all.

    Fail-closed: ``.run()`` raises ``MissingOrganisationContextError`` if no
    context is bound at runtime (T1-M1 / ADR-012 §1b).
    """

    def __init__(
        self,
        *,
        base_writer: Neo4jWriter,
        graph_id: str,
        context: OrganisationContext | None = None,  # noqa: ARG002 — deprecated; ignored
        user_id: str | None = None,
        ingestion_source: str | None = None,
        citation: Citation | None = None,
    ) -> None:
        super().__init__(
            base_writer=base_writer,
            graph_id=graph_id,
            user_id=user_id,
            ingestion_source=ingestion_source,
            citation=citation,
        )

    def _injected_properties(self, now: datetime) -> dict[str, object]:
        """Add ``organisation_id`` (outermost scope) to the legacy stamps.

        Key from the substrate's canonical constant (AC1); value from the
        bound seam (AC2). Raises ``MissingOrganisationContextError`` when no
        context is bound at run time — fail-closed (T1-M1).
        """
        props = super()._injected_properties(now)
        props[access.ORGANISATION_ID_PROPERTY] = access.enforced_organisation_id()
        return props
