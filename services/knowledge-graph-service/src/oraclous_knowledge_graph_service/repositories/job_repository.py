"""Ingestion-job repository (ORAA-4 §21 repositories layer — the only `ingestion_jobs` SQL).

Org-scoped fail-closed via `oraclous_substrate.access.enforced_organisation_id()` (ADR-006), exactly
like the graph repository. Used from the request path (create/get/list) and from the Celery worker
(load payload + update_status) — both inside a bound org context.
"""

from __future__ import annotations

import uuid

from oraclous_substrate.access import enforced_organisation_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord, IngestionPayload
from oraclous_knowledge_graph_service.repositories.models import IngestionJob


def _to_record(row: IngestionJob) -> IngestionJobRecord:
    return IngestionJobRecord(
        id=row.id,
        organisation_id=row.organisation_id,
        graph_id=row.graph_id,
        source_type=row.source_type,
        filename=row.filename,
        status=row.status,
        progress=row.progress,
        error_message=row.error_message,
        extracted_entities=row.extracted_entities,
        extracted_relationships=row.extracted_relationships,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class IngestionJobRepository:
    """Org-scoped CRUD over `ingestion_jobs`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _org(self) -> uuid.UUID:
        return uuid.UUID(enforced_organisation_id())

    async def create(
        self,
        *,
        graph_id: uuid.UUID,
        source_type: str,
        filename: str | None,
        source_content: str | None,
        recipe_id: str | None = None,
    ) -> IngestionJobRecord:
        row = IngestionJob(
            organisation_id=self._org(),
            graph_id=graph_id,
            source_type=source_type,
            filename=filename,
            source_content=source_content,
            recipe_id=recipe_id,
            status="pending",
            progress=0,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def get(self, job_id: uuid.UUID) -> IngestionJobRecord | None:
        row = await self._fetch(job_id)
        return _to_record(row) if row else None

    async def list_for_graph(self, graph_id: uuid.UUID) -> list[IngestionJobRecord]:
        stmt = (
            select(IngestionJob)
            .where(
                IngestionJob.organisation_id == self._org(),
                IngestionJob.graph_id == graph_id,
            )
            .order_by(IngestionJob.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(r) for r in rows]

    async def load_payload(self, job_id: uuid.UUID) -> IngestionPayload | None:
        row = await self._fetch(job_id)
        if row is None:
            return None
        return IngestionPayload(
            graph_id=row.graph_id,
            source_type=row.source_type,
            filename=row.filename,
            source_content=row.source_content,
            recipe_id=row.recipe_id,
        )

    async def update_status(
        self,
        job_id: uuid.UUID,
        *,
        status: str,
        progress: int | None = None,
        error_message: str | None = None,
        extracted_entities: int | None = None,
        extracted_relationships: int | None = None,
    ) -> None:
        row = await self._fetch(job_id)
        if row is None:
            return
        row.status = status
        if progress is not None:
            row.progress = progress
        if error_message is not None:
            row.error_message = error_message
        if extracted_entities is not None:
            row.extracted_entities = extracted_entities
        if extracted_relationships is not None:
            row.extracted_relationships = extracted_relationships
        await self._session.flush()

    async def _fetch(self, job_id: uuid.UUID) -> IngestionJob | None:
        stmt = select(IngestionJob).where(
            IngestionJob.id == job_id,
            IngestionJob.organisation_id == self._org(),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
