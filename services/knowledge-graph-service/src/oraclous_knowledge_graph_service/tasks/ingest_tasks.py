"""The document ingest task (R3.5-P1-S2).

THE org-context gotcha: a Celery worker has NO HTTP request, so the governance ContextVar is unbound
and the substrate fails closed (every org-scoped read/write would raise). The task therefore (a)
carries organisation_id as an explicit JSON arg — the only channel across the broker — and (b) binds
it via `use_organisation_context` BEFORE any substrate call. Everything inside that block (Postgres
job updates, Neo4j writes) then reads the org from the contextvar exactly as in a request.

NullPool engine + a task-scoped Neo4j driver, both disposed per task (ADR-012 worker invariant).
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_sessionmaker, make_worker_engine
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    GraphWriteRepository,
)
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.services.embedder import make_embedder
from oraclous_knowledge_graph_service.services.ingestion_service import IngestionService
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
                write_repo = GraphWriteRepository(driver, database=settings.neo4j_database)
                ingestion = IngestionService(write_repo, make_embedder(settings))
                result = await ingestion.ingest(
                    graph_id=str(payload.graph_id),
                    document=payload.filename or "inline",
                    data=data,
                    source_type=payload.source_type,
                )
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
                    extracted_entities=result.nodes,
                    extracted_relationships=result.relationships,
                )
                await session.commit()
            return {
                "status": "completed",
                "job_id": job_id_s,
                "nodes": result.nodes,
                "relationships": result.relationships,
                "chunks": result.chunks,
            }
        finally:
            driver.close()
            await engine.dispose()
