"""Ingestion use-case (ORAA-4 §21 services layer) — extract → chunk → embed → (extract) → write.

The text/document spine: turn raw bytes into a `:Document`/`:Chunk` graph via the org-scoped writer.

Two modes, selected by `KGS_EXTRACTOR`:
  - `null` (default, key-free CI): lexical only — `:Document` + `:Chunk` with deterministic
    embeddings. No LLM, no entities. `extractor` is None.
  - `openai`: additionally runs LLM entity + relationship extraction over the chunks, so the same
    free text also materialises real domain `:Entity` nodes + their relationships, linked
    (`FROM_CHUNK`) to the chunk they came from. `extractor` is an injectable `EntityExtractor`.

The org scope is bound by the caller (HTTP request or the worker's `use_organisation_context`); the
write repository stamps `organisation_id` on every node/rel (an LLM-extracted org id cannot
redirect a write to another tenant).
"""

from __future__ import annotations

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


class IngestionService:
    def __init__(
        self,
        write_repo: GraphWriteRepository,
        embedder: Embedder,
        extractor: EntityExtractor | None = None,
    ) -> None:
        self._write_repo = write_repo
        self._embedder = embedder
        self._extractor = extractor

    async def ingest(
        self, *, graph_id: str, document: str, data: bytes, source_type: str | None
    ) -> WriteResult:
        try:
            text, _meta = extract_text(data=data, filename=document, source_type=source_type)
        except ExtractionError as exc:
            raise IngestionError(str(exc)) from exc
        chunks = chunk_text(text)
        if not chunks:
            raise IngestionError("no chunks produced from extracted text")
        embeddings = self._embedder.embed(chunks)
        entity_graph = None
        if self._extractor is not None:
            # Link extracted entities to the SAME deterministic chunk ids the writer builds.
            chunk_ids = chunk_node_ids(graph_id=graph_id, document=document, count=len(chunks))
            entity_graph = await self._extractor.extract(chunks=chunks, chunk_ids=chunk_ids)
        return await self._write_repo.write_document(
            graph_id=graph_id,
            document=document,
            chunks=chunks,
            embeddings=embeddings,
            entity_graph=entity_graph,
        )
