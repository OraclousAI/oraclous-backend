"""The document ingest task (R3.5-P1-S2).

THE org-context gotcha: a Celery worker has NO HTTP request, so the governance ContextVar is unbound
and the substrate fails closed (every org-scoped read/write would raise). The task therefore (a)
carries organisation_id as an explicit JSON arg — the only channel across the broker — and (b) binds
it via `use_organisation_context` BEFORE any substrate call. Everything inside that block (Postgres
job updates, Neo4j writes) then reads the org from the contextvar exactly as in a request.

NullPool engine + a task-scoped Neo4j driver, both disposed per task (ADR-012 worker invariant).
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Any

from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_sessionmaker, make_worker_engine
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.domain.ontology import Ontology
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.repositories.recipe_repository import RecipeRepository
from oraclous_knowledge_graph_service.services.code_ingestion_service import (
    CodeIngestionService,
    is_code,
)
from oraclous_knowledge_graph_service.services.embedder import make_embedder
from oraclous_knowledge_graph_service.services.entity_extractor import make_extractor
from oraclous_knowledge_graph_service.services.ingestion_service import IngestionService
from oraclous_knowledge_graph_service.services.structured_ingestion_service import (
    StructuredIngestionService,
    is_structured,
)
from oraclous_knowledge_graph_service.tasks.celery_app import AsyncTaskExecutor, celery_app


@celery_app.task(bind=True, name="kgs.ingest_document")
def ingest_document_task(self, job_id: str, organisation_id: str) -> dict[str, Any]:  # noqa: ARG001
    return AsyncTaskExecutor.run_async_task(_ingest_async, job_id, organisation_id)


async def _ingest_async(job_id_s: str, organisation_id_s: str) -> dict[str, Any]:
    settings = get_settings()
    job_id = uuid.UUID(job_id_s)
    org_id = uuid.UUID(organisation_id_s)
    context = OrganisationContext(
        organisation_id=org_id,
        principal_id=org_id,
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )
    with use_organisation_context(context):
        engine = make_worker_engine()
        maker = make_sessionmaker(engine)
        driver = make_neo4j_driver(settings)
        try:
            async with maker() as session:
                payload = await IngestionJobRepository(session).load_payload(job_id)
                if payload is None:
                    return {"status": "missing", "job_id": job_id_s}
                await IngestionJobRepository(session).update_status(
                    job_id, status="running", progress=10
                )
                await session.commit()

            try:
                data = base64.b64decode(payload.source_content or "")
                if is_structured(payload.source_type):
                    summary = await _ingest_structured(
                        driver=driver, maker=maker, settings=settings, payload=payload, data=data
                    )
                elif is_code(payload.source_type):
                    summary = await _ingest_code(
                        driver=driver, settings=settings, payload=payload, data=data
                    )
                else:
                    write_repo = GraphWriteRepository(driver, database=settings.neo4j_database)
                    ingestion = IngestionService(
                        write_repo, make_embedder(settings), make_extractor(settings)
                    )
                    result = await ingestion.ingest(
                        graph_id=str(payload.graph_id),
                        document=payload.filename or "inline",
                        data=data,
                        source_type=payload.source_type,
                    )
                    # Honest extracted counts: the LLM-extracted entities + their entity↔entity
                    # relationships (0 in null mode), NOT the lexical Document/Chunk node total.
                    summary = {
                        "entities": result.entities,
                        "relationships": result.entity_relationships,
                        "detail": {
                            "nodes": result.nodes,
                            "relationships": result.relationships,
                            "chunks": result.chunks,
                            "extracted_entities": result.entities,
                            "extracted_relationships": result.entity_relationships,
                        },
                    }
            except Exception as exc:
                async with maker() as session:
                    await IngestionJobRepository(session).update_status(
                        job_id, status="failed", progress=0, error_message=str(exc)
                    )
                    await session.commit()
                raise

            async with maker() as session:
                await IngestionJobRepository(session).update_status(
                    job_id,
                    status="completed",
                    progress=100,
                    extracted_entities=summary["entities"],
                    extracted_relationships=summary["relationships"],
                )
                await session.commit()
            return {"status": "completed", "job_id": job_id_s, **summary}
        finally:
            driver.close()
            await engine.dispose()


async def _ingest_structured(*, driver, maker, settings, payload, data: bytes) -> dict[str, Any]:
    """CSV/JSON: decompose -> recipe (stored or default) -> engine over the org-scoped writer.

    Applies the graph's ontology (STRICT/COERCE) + temporal passthrough (valid_from/valid_to/
    event_time) at projection time."""
    text = data.decode("utf-8", errors="replace")
    recipe = None
    async with maker() as session:
        if payload.recipe_id:
            recipe = await RecipeRepository(session).get_latest(payload.recipe_id)
            if recipe is None:
                raise RuntimeError(f"recipe {payload.recipe_id!r} not found")
        ontology_data = await GraphRepository(session).get_ontology(payload.graph_id)
    temporal = {
        "valid_from": payload.valid_from,
        "valid_to": payload.valid_to,
        "event_time": payload.event_time,
    }
    service = StructuredIngestionService(
        driver=driver,
        organisation_id=enforced_organisation_id(),
        database=settings.neo4j_database,
    )
    result = await asyncio.to_thread(
        service.ingest,
        graph_id=str(payload.graph_id),
        document=payload.filename or "inline",
        text=text,
        source_type=payload.source_type,
        recipe=recipe,
        ontology=Ontology.of(ontology_data),
        temporal=temporal,
    )
    return {
        "entities": result["nodes_written"] + result["containers_written"],
        "relationships": result["edges_written"],
        "detail": result,
    }


async def _ingest_code(*, driver, settings, payload, data: bytes) -> dict[str, Any]:
    """Code (zip / single file): tree-sitter parse -> :File/:Function/:Class via the code writer."""
    service = CodeIngestionService(
        driver=driver, organisation_id=enforced_organisation_id(), database=settings.neo4j_database
    )
    counts = await asyncio.to_thread(
        service.ingest,
        graph_id=str(payload.graph_id),
        document=payload.filename or "code.zip",
        data=data,
    )
    entities = counts["files"] + counts["functions"] + counts["classes"] + counts["variables"]
    relationships = counts["calls"] + counts["imports"] + counts["inherits"]
    return {"entities": entities, "relationships": relationships, "detail": counts}
