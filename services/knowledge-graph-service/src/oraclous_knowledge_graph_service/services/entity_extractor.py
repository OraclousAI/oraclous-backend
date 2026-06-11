"""LLM entity + relationship extraction seam (ORAA-4 §21 services layer).

The non-lexical half of free-text ingestion. When `KGS_EXTRACTOR=openai`, this turns each ingested
text chunk into real domain entities + the relationships between them, so a Markdown/PDF/text file
yields not just `:Document` + `:Chunk` but the `:Entity` (domain-labelled) graph the text describes.

It wraps the already-vendored `neo4j_graphrag` `LLMEntityRelationExtractor` (the same library used
on the lexical side) rather than hand-rolling prompt/JSON parsing. The LLM is an injectable
`LLMInterface`, so unit tests pass a fake that returns a fixed entity/relation set with no network.

The extracted entities are linked to the SAME deterministic `:Chunk` nodes the lexical writer
builds (`FROM_CHUNK`, entity → chunk), so the graph is navigable from a chunk to what it mentions.
Node ids are namespaced by the chunk id (mirrors the library's own cross-chunk id contract), so the
same entity mentioned in two chunks yields two nodes the resolver can later merge — and a re-ingest
of the same chunk text + ids is an idempotent MERGE.

Org-stamping is NOT done here: the returned `Neo4jGraph` is written through the
`OrganisationScopedKGWriter`, which unconditionally stamps `organisation_id`/`graph_id` on every
node + relationship (so an LLM-extracted `organisation_id` can never redirect a write — T1 defence).
"""

from __future__ import annotations

import logging

from neo4j_graphrag.experimental.components.entity_relation_extractor import (
    LLMEntityRelationExtractor,
    OnError,
)
from neo4j_graphrag.experimental.components.schema import GraphSchema
from neo4j_graphrag.experimental.components.types import (
    LexicalGraphConfig,
    Neo4jGraph,
    Neo4jRelationship,
    TextChunk,
)
from neo4j_graphrag.llm import LLMInterface

from oraclous_knowledge_graph_service.core.config import Settings

logger = logging.getLogger(__name__)

# entity → chunk edge type; matches the library's lexical default so reads are uniform.
_FROM_CHUNK = LexicalGraphConfig().node_to_chunk_relationship_type


class EntityExtractor:
    """Extract domain entities + relationships from chunks via an injectable LLM.

    `llm` is any `neo4j_graphrag` `LLMInterface` (the real one points at OpenRouter; tests inject a
    fake). `schema` is open by default — no predefined ontology, so the LLM discovers entity +
    relation types from the text. When a graph carries a TYPED ontology, the caller compiles it (see
    `domain.extraction_schema`) to a hard `GraphSchema` (the extractor enforces it) plus a
    `prompt_prefix` of soft steering (domain/density/focus/ignore) — BOTH flow into every
    `extract_for_chunk`. `on_error=IGNORE` keeps one malformed-JSON chunk from sinking the whole
    document (that chunk simply yields no entities).
    """

    def __init__(
        self,
        *,
        llm: LLMInterface,
        max_concurrency: int = 5,
        schema: GraphSchema | None = None,
        prompt_prefix: str = "",
    ) -> None:
        # create_lexical_graph=False: we own the lexical (:Document/:Chunk) graph on the write side
        # with deterministic ids + embeddings; the extractor returns ONLY the entity sub-graph.
        self._extractor = LLMEntityRelationExtractor(
            llm=llm,
            create_lexical_graph=False,
            on_error=OnError.IGNORE,
            max_concurrency=max_concurrency,
        )
        self._schema = schema or GraphSchema(node_types=())
        self._prompt_prefix = prompt_prefix

    async def extract(self, *, chunks: list[str], chunk_ids: list[str]) -> Neo4jGraph:
        """Run extraction over the chunk texts and return their entity sub-graph.

        Returns a `Neo4jGraph` of extracted entity nodes, the extracted entity↔entity relationships,
        and a `FROM_CHUNK` edge from every extracted entity to the deterministic chunk it came from
        (`chunk_ids[i]` is the lexical-writer id for `chunks[i]`). No `:Document`/`:Chunk` nodes are
        produced here — those are the write side's job.
        """
        if len(chunks) != len(chunk_ids):
            raise ValueError("chunks and chunk_ids must be the same length")

        combined = Neo4jGraph()
        for index, (text, chunk_id) in enumerate(zip(chunks, chunk_ids, strict=True)):
            # The library namespaces extracted node ids by chunk.chunk_id (its `update_ids`). Using
            # the deterministic lexical chunk id as chunk_id makes the entity ids deterministic too
            # (idempotent re-ingest) AND lets us link entities to the real chunk node below.
            chunk = TextChunk(text=text, index=index, uid=chunk_id)
            # schema = hard ontology (enforced); prompt_prefix = soft steering (formatted into the
            # extractor's `{examples}` prompt slot). Both empty/open by default.
            chunk_graph = await self._extractor.extract_for_chunk(
                self._schema, self._prompt_prefix, chunk
            )
            self._extractor.update_ids(chunk_graph, chunk)
            self._link_entities_to_chunk(chunk_graph, chunk_id)
            combined.nodes.extend(chunk_graph.nodes)
            combined.relationships.extend(chunk_graph.relationships)

        logger.info(
            "EntityExtractor: %d entities, %d relationships from %d chunks",
            len(combined.nodes),
            len(combined.relationships),
            len(chunks),
        )
        return combined

    @staticmethod
    def _link_entities_to_chunk(chunk_graph: Neo4jGraph, chunk_id: str) -> None:
        """Add a FROM_CHUNK edge from each extracted entity to its source chunk (in place)."""
        for node in chunk_graph.nodes:
            chunk_graph.relationships.append(
                Neo4jRelationship(start_node_id=node.id, end_node_id=chunk_id, type=_FROM_CHUNK)
            )


def make_extractor(
    settings: Settings,
    *,
    schema: GraphSchema | None = None,
    prompt_prefix: str = "",
) -> EntityExtractor | None:
    """Build the entity extractor from config, or None when LLM extraction is off (`null`).

    `schema`/`prompt_prefix` carry a graph's compiled TYPED ontology (from
    `domain.extraction_schema`): `schema` is the hard `GraphSchema` the extractor enforces;
    `prompt_prefix` is the soft steering. Both default to open/empty (free-form extraction).

    Fail-closed: `KGS_EXTRACTOR=openai` with no API key configured raises — it never silently falls
    back to lexical-only and reports entities it did not extract.
    """
    if settings.extractor == "null":
        return None
    if not settings.openai_api_key:
        raise RuntimeError("KGS_EXTRACTOR=openai requires KGS_OPENAI_API_KEY")
    # Lazy import so the key-free `null` path (CI default) never imports openai.
    from neo4j_graphrag.llm import OpenAILLM

    llm = OpenAILLM(
        model_name=settings.extractor_model,
        # JSON-object response so the extractor's JSON parse is reliable across providers.
        model_params={"temperature": 0.0, "response_format": {"type": "json_object"}},
        # kwargs flow to the openai client init -> point it at OpenRouter (or any compatible base).
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    return EntityExtractor(
        llm=llm,
        max_concurrency=settings.extractor_max_concurrency,
        schema=schema,
        prompt_prefix=prompt_prefix,
    )
