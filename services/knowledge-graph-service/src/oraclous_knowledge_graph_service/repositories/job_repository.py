"""Ingestion-job repository (repositories layer — the only `ingestion_jobs` SQL).

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
        valid_from: str | None = None,
        valid_to: str | None = None,
        event_time: str | None = None,
    ) -> IngestionJobRecord:
        row = IngestionJob(
            organisation_id=self._org(),
            graph_id=graph_id,
            source_type=source_type,
            filename=filename,
            source_content=source_content,
            recipe_id=recipe_id,
            valid_from=valid_from,
            valid_to=valid_to,
            event_time=event_time,
            status="pending",
            progress=0,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def commit(self) -> None:
        """Durably commit the current unit of work.

        The request-scoped `session_scope` only commits AFTER the route returns, so a row created
        in `create` is still uncommitted when the service enqueues the Celery task — a separate
        worker session would not see it (read-after-write race → silent 'missing' drop, #267).
        `JobService.submit` calls this before enqueueing so the row is visible cross-session first.
        """
        await self._session.commit()

    async def get(self, job_id: uuid.UUID) -> IngestionJobRecord | None:
        row = await self._fetch(job_id)
        return _to_record(row) if row else None

    async def get_source_content(self, job_id: uuid.UUID) -> str | None:
        """The verbatim ingested content of one artifact (org-scoped via ``_fetch``) — what
        ``/v1/artifacts/{id}`` serves; None if the row is absent / has no stored content (#543)."""
        row = await self._fetch(job_id)
        return row.source_content if row else None

    async def list_for_graph(
        self, graph_id: uuid.UUID, *, exclude_source_types: tuple[str, ...] = ()
    ) -> list[IngestionJobRecord]:
        stmt = (
            select(IngestionJob)
            .where(
                IngestionJob.organisation_id == self._org(),
                IngestionJob.graph_id == graph_id,
            )
            .order_by(IngestionJob.created_at.desc())
        )
        if exclude_source_types:
            stmt = stmt.where(IngestionJob.source_type.notin_(exclude_source_types))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(r) for r in rows]

    async def latest_by_source_type(
        self, graph_id: uuid.UUID, *, source_type: str
    ) -> IngestionJobRecord | None:
        """The most-recent job of ``source_type`` for this org+graph (None if none). Used by the
        community-status read to surface a running/failed async detect that has no substrate yet."""
        stmt = (
            select(IngestionJob)
            .where(
                IngestionJob.organisation_id == self._org(),
                IngestionJob.graph_id == graph_id,
                IngestionJob.source_type == source_type,
            )
            .order_by(IngestionJob.created_at.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_record(row) if row else None

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
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            event_time=row.event_time,
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
