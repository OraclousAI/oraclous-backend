"""Ingestion use-case (services layer) — extract → chunk → embed → (extract) → write.

The text/document spine: turn raw bytes into a `:Document`/`:Chunk` graph via the org-scoped writer.

Two modes, selected by `KGS_EXTRACTOR`:
  - `null` (default, key-free CI): lexical only — `:Document` + `:Chunk` with deterministic
    embeddings. No LLM, no entities. `extractor` is None.
  - `openai`: additionally runs LLM entity + relationship extraction over the chunks, so the same
    free text also materialises real domain `:Entity` nodes + their relationships, linked
    (`FROM_CHUNK`) to the chunk they came from. `extractor` is an injectable `EntityExtractor`.

Slice B: when the graph carries a TYPED ontology, the extractor is built with a hard `GraphSchema`
(so the LLM extracts conforming types) AND, post-extraction, the SAME strict/coerce enforcement the
structured path uses is applied here too (`resolve_label`): a strict graph drops an off-ontology
entity (and its incident edges) before the write; a coerce graph remaps a near-match label. So
free-text ingestion can never add an off-ontology node to a strict graph.

The org scope is bound by the caller (HTTP request or the worker's `use_organisation_context`); the
write repository stamps `organisation_id` on every node/rel (an LLM-extracted org id cannot
redirect a write to another tenant).
"""

from __future__ import annotations

from dataclasses import dataclass

from neo4j_graphrag.experimental.components.types import Neo4jGraph
from oraclous_citation import (
    AGENT_SOURCE_SYSTEM,
    UPLOAD_SOURCE_SYSTEM,
    Citation,
    SourceRef,
    mint_citation,
)

from oraclous_knowledge_graph_service.domain.artifact_naming import AGENT_PRODUCER_KINDS
from oraclous_knowledge_graph_service.domain.ontology import Ontology, resolve_label
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
    WriteResult,
    chunk_node_ids,
)
from oraclous_knowledge_graph_service.services.chunker import chunk_text
from oraclous_knowledge_graph_service.services.embedder import Embedder
from oraclous_knowledge_graph_service.services.entity_extractor import EntityExtractor
from oraclous_knowledge_graph_service.services.extractors import ExtractionError, extract_text


class IngestionError(Exception):
    """Ingestion failed (no extractable text, or a downstream write error)."""


@dataclass
class _Enforced:
    """The ontology-enforced entity sub-graph plus the strict-drop / coerce-remap counts."""

    graph: Neo4jGraph
    violations: int
    coercions: int


def enforce_ontology(entity_graph: Neo4jGraph, ontology: Ontology | None) -> _Enforced:
    """Apply the graph's ontology to an extracted entity sub-graph (pure; reuses `resolve_label`).

    - open / None: passthrough (no change).
    - strict: drop every entity whose label is off-ontology, and any relationship incident to a
      dropped node (so no dangling edge survives). Counts each drop as a violation.
    - coerce: remap a near-match label to its allowed form (counted as a coercion); a too-far label
      is dropped (counted as a violation), like strict.

    #650: an EMPTY allow-list under strict/coerce is NOT passthrough — it denies every entity (the
    drop loop below does that via `resolve_label`), so a label-less strict ontology fails closed.
    """
    if ontology is None or ontology.mode == "open":
        return _Enforced(entity_graph, 0, 0)

    kept_nodes = []
    dropped_ids: set[str] = set()
    violations = 0
    coercions = 0
    for node in entity_graph.nodes:
        resolved, coerced = resolve_label(ontology, node.label)
        if resolved is None:
            dropped_ids.add(node.id)
            violations += 1
            continue
        if coerced:
            node.label = resolved
            coercions += 1
        kept_nodes.append(node)

    kept_rels = [
        rel
        for rel in entity_graph.relationships
        if rel.start_node_id not in dropped_ids and rel.end_node_id not in dropped_ids
    ]
    return _Enforced(Neo4jGraph(nodes=kept_nodes, relationships=kept_rels), violations, coercions)


def _mint_for_ingest(
    *,
    source: SourceRef | None,
    job_id: str | None,
    document: str,
    content: str,
    producer_kind: str | None = None,
) -> Citation | None:
    """Mint the citation this ingest will stamp, from the connector's source or a fallback.

    Minting happens HERE and not in the writer: §CITE mints once, at the tool-execution boundary,
    through a function general over a ``SourceRef``. The writer sits below that boundary and holds
    chunks rather than the document content, so it could not compute the content-hash fallback even
    if it were the right place.

    ``content`` is the EXTRACTED text, not the raw bytes: it is what the record actually carries,
    so re-saving a PDF with different container metadata does not read as a new revision.

    Returns ``None`` only when there is no job id to attribute an upload to — a direct service-level
    call outside the job pipeline. The record is then ``citation: null``, which is the Contract's
    defined meaning for a record with no source identity.

    §CITE: an upload citation carries the ingest job id as its ``source_id``, the content hash as
    its revision, and ``url: null`` — no route serves an uploaded document back yet, and claiming a
    URL that does not resolve would be the same fiction the Contract exists to reject.

    ``producer_kind`` (§CITE rev4 decision 7) is what separates the two sourceless cases. Content an
    AGENT wrote is stamped ``agent``, so a reader can tell a cited passage was written by an agent
    rather than read from the world; without it an agent can write a file, retrieve it and cite it,
    and the citation is indistinguishable from a document a person actually brought. A real
    ``source`` still WINS — an agent that reads a genuine GitHub file has a genuine origin, and
    recording authorship must never destroy it. Anything unrecognised falls back to ``upload``, so
    the fail-safe direction is the old behaviour rather than a silent agent write.
    """
    if source is not None:
        return mint_citation(source, content=content)
    if job_id is None:
        return None
    source_system = (
        AGENT_SOURCE_SYSTEM if producer_kind in AGENT_PRODUCER_KINDS else UPLOAD_SOURCE_SYSTEM
    )
    return mint_citation(
        SourceRef(
            source_system=source_system,
            source_id=job_id,
            url=None,
            title=document or None,
        ),
        content=content,
    )


class IngestionService:
    def __init__(
        self,
        write_repo: GraphWriteRepository,
        embedder: Embedder,
        extractor: EntityExtractor | None = None,
        ontology: Ontology | None = None,
    ) -> None:
        self._write_repo = write_repo
        self._embedder = embedder
        self._extractor = extractor
        self._ontology = ontology

    async def ingest(
        self,
        *,
        graph_id: str,
        document: str,
        data: bytes,
        source_type: str | None,
        document_key: str | None = None,
        source: SourceRef | None = None,
        job_id: str | None = None,
        producer_kind: str | None = None,
    ) -> WriteResult:
        """Chunk, embed, extract and write one document.

        ``document`` is the human name — it drives format detection (its extension) and becomes the
        node's title. ``document_key`` (#728) is the node's IDENTITY when supplied: an agent
        artifact passes its own job id, so two members of one run writing under similar names can
        never merge onto a single node. Omitted → the name IS the key, which is what keeps a user
        upload's re-ingest-the-same-path REPLACE behaviour (#522) intact.

        ``producer_kind`` (#786) is WHO wrote the content, as the job row recorded it. It decides
        the citation when nothing else supplied a source: an agent's own writing is stamped
        ``agent`` rather than passed off as a user upload.
        """
        try:
            text, _meta = extract_text(data=data, filename=document, source_type=source_type)
        except ExtractionError as exc:
            raise IngestionError(str(exc)) from exc
        chunks = chunk_text(text)
        if not chunks:
            raise IngestionError("no chunks produced from extracted text")
        citation = _mint_for_ingest(
            source=source,
            job_id=job_id,
            document=document,
            content=text,
            producer_kind=producer_kind,
        )
        embeddings = self._embedder.embed(chunks)
        entity_graph = None
        violations = 0
        coercions = 0
        key = document_key or document
        if self._extractor is not None:
            # Link extracted entities to the SAME deterministic chunk ids the writer builds.
            chunk_ids = chunk_node_ids(graph_id=graph_id, document=key, count=len(chunks))
            extracted = await self._extractor.extract(chunks=chunks, chunk_ids=chunk_ids)
            # Hard schema steers the LLM; this strict/coerce pass is the belt-and-braces guarantee
            # that a strict graph never gains an off-ontology node from free text.
            enforced = enforce_ontology(extracted, self._ontology)
            entity_graph = enforced.graph
            violations = enforced.violations
            coercions = enforced.coercions
        return await self._write_repo.write_document(
            graph_id=graph_id,
            document=key,
            title=document,
            chunks=chunks,
            embeddings=embeddings,
            entity_graph=entity_graph,
            ontology_violations=violations,
            ontology_coercions=coercions,
            citation=citation,
        )
