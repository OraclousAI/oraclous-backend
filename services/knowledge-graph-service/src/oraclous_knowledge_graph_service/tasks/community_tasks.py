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
import logging
import uuid
from typing import Any

from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_sessionmaker, make_worker_engine
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.core.redis import make_redis_lock_client
from oraclous_knowledge_graph_service.domain.community import (
    DEFAULT_MIN_ENTITIES,
    DetectionInProgress,
)
from oraclous_knowledge_graph_service.repositories.community_repository import CommunityRepository
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.services.analytics_service import decode_detect_params
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
        lock_client = make_redis_lock_client(settings)
        try:
            async with maker() as session:
                jobs = IngestionJobRepository(session)
                payload = await jobs.load_payload(job_id)
                if payload is None:
                    raise JobNotVisibleYet(job_id_s)
                graph_id = str(payload.graph_id)
                # The request params (min_entities/force_rebuild) ride on source_content so the
                # worker applies the SAME floor/skip semantics as the inline path (not defaults).
                min_entities, force_rebuild = decode_detect_params(payload.source_content)
                await jobs.update_status(job_id, status="running", progress=10)
                await session.commit()

            try:
                # The worker shares the per-(org,graph) detect lock with the inline path (passed the
                # sync Redis client) so the two never race the destructive clear+rebuild.
                repo = CommunityRepository(
                    driver, database=settings.neo4j_database, lock_client=lock_client
                )
                floor = DEFAULT_MIN_ENTITIES if min_entities is None else min_entities
                entity_count = await asyncio.to_thread(repo.count_entities, graph_id=graph_id)
                cap = settings.community_max_entities
                skipped_reason: str | None = None
                if entity_count < floor:
                    skipped_reason = f"entity count {entity_count} < minimum {floor}"
                elif cap and entity_count > cap:
                    skipped_reason = f"entity count {entity_count} exceeds maximum {cap}"
                elif not force_rebuild:
                    existing, _, _ = await asyncio.to_thread(repo.status, graph_id=graph_id)
                    if existing > 0:
                        skipped_reason = "communities already detected; pass force_rebuild"

                total = 0
                summarized = 0
                if skipped_reason is None:
                    try:
                        levels = await asyncio.to_thread(repo.detect, graph_id=graph_id)
                    except DetectionInProgress:
                        skipped_reason = "community detection already in progress"
                        levels = {}
                    total = sum(len(groups) for groups in levels.values())
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
                    error_message=skipped_reason,
                    extracted_entities=total,
                    extracted_relationships=summarized,
                )
                await session.commit()
            return {
                "status": "skipped" if skipped_reason else "completed",
                "job_id": job_id_s,
                "total_communities": total,
                "summarized": summarized,
                "reason": skipped_reason,
            }
        finally:
            driver.close()
            await engine.dispose()
            if lock_client is not None:
                try:
                    lock_client.close()
                except Exception as exc:  # noqa: BLE001 — best-effort close of advisory lock client
                    logging.getLogger(__name__).debug("lock client close skipped: %s", exc)
