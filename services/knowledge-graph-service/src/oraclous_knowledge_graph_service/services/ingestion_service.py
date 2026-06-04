"""Ingestion use-case (ORAA-4 §21 services layer) — extract → chunk → embed → write.

The deterministic, key-free text/document spine: turn raw bytes into a lexical `:Document`/`:Chunk`
graph via the org-scoped writer. No LLM entity-extraction on this path (the `null` extractor seam);
free text yields `:Document` + `:Chunk` with deterministic embeddings. The org scope is bound
by the caller (HTTP request or the worker's `use_organisation_context`).
"""

from __future__ import annotations

from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
    WriteResult,
)
from oraclous_knowledge_graph_service.services.chunker import chunk_text
from oraclous_knowledge_graph_service.services.embedder import Embedder
from oraclous_knowledge_graph_service.services.extractors import ExtractionError, extract_text


class IngestionError(Exception):
    """Ingestion failed (no extractable text, or a downstream write error)."""


class IngestionService:
    def __init__(self, write_repo: GraphWriteRepository, embedder: Embedder) -> None:
        self._write_repo = write_repo
        self._embedder = embedder

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
        return await self._write_repo.write_document(
            graph_id=graph_id, document=document, chunks=chunks, embeddings=embeddings
        )
