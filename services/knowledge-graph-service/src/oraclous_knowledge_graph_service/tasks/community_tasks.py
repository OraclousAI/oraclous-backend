"""The community-detection Celery task (#303).

Mirrors ``ingest_tasks.ingest_document_task`` exactly — the same org-context bridge, the same
NullPool-engine + task-scoped Neo4j driver disposed per task (ADR-012 worker invariant), the same
``JobNotVisibleYet`` read-after-write retry (#267). A Celery worker has NO request, so the
organisation_id is carried as an explicit JSON arg and re-bound via ``use_organisation_context``
BEFORE any substrate call.

Work: run in-DB GDS Louvain detection across the 5 resolutions (``CommunityRepository.detect``),
then — when LLM summarisation is enabled (``KGS_EXTRACTOR=openai``) — summarise the detected
communities. Both read the org from the contextvar exactly as a request would. The job row is the
existing ``ingestion_jobs`` row created with ``source_type='community_detect'``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_sessionmaker, make_worker_engine
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.domain.community import DEFAULT_RESOLUTIONS
from oraclous_knowledge_graph_service.repositories.community_repository import CommunityRepository
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.services.community_summarizer import make_summarizer
from oraclous_knowledge_graph_service.tasks.celery_app import AsyncTaskExecutor, celery_app
from oraclous_knowledge_graph_service.tasks.ingest_tasks import JobNotVisibleYet


@celery_app.task(
    bind=True,
    name="kgs.detect_communities",
    autoretry_for=(JobNotVisibleYet,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    max_retries=5,
)
def detect_communities_task(self, job_id: str, organisation_id: str) -> dict[str, Any]:  # noqa: ARG001
    return AsyncTaskExecutor.run_async_task(_detect_async, job_id, organisation_id)


async def _detect_async(job_id_s: str, organisation_id_s: str) -> dict[str, Any]:
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
                jobs = IngestionJobRepository(session)
                payload = await jobs.load_payload(job_id)
                if payload is None:
                    raise JobNotVisibleYet(job_id_s)
                graph_id = str(payload.graph_id)
                await jobs.update_status(job_id, status="running", progress=10)
                await session.commit()

            try:
                repo = CommunityRepository(driver, database=settings.neo4j_database)
                levels = await asyncio.to_thread(
                    repo.detect, graph_id=graph_id, resolutions=DEFAULT_RESOLUTIONS
                )
                total = sum(len(groups) for groups in levels.values())
                summarized = 0
                summarizer = make_summarizer(settings, repo=repo)
                if summarizer is not None and total:
                    results = await summarizer.summarize_graph(graph_id=graph_id)
                    summarized = len(results)
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
                    extracted_entities=total,
                    extracted_relationships=summarized,
                )
                await session.commit()
            return {
                "status": "completed",
                "job_id": job_id_s,
                "total_communities": total,
                "summarized": summarized,
            }
        finally:
            driver.close()
            await engine.dispose()
