"""Ingestion-job use-cases (services layer).

Owner-gated (a job is only visible/creatable on a graph the caller owns — reuses GraphService's
gate, so cross-user/cross-org → 404) and org-scoped (the repository enforces the org). `submit`
creates the job row, COMMITS it (so the worker's separate session can see it — see #267), then
enqueues the Celery task, passing organisation_id explicitly so the worker can re-bind the org
context across the process boundary. `enqueue` is injected so the service stays testable without a
broker.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable

from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.domain.community import COMMUNITY_DETECT_SOURCE_TYPE
from oraclous_knowledge_graph_service.domain.job import IngestionJobRecord
from oraclous_knowledge_graph_service.repositories.job_repository import IngestionJobRepository
from oraclous_knowledge_graph_service.services.graph_service import GraphService

EnqueueFn = Callable[[str, str], object]


class JobNotFound(Exception):
    """Raised when a job is not visible to the caller. Maps to 404."""


class JobService:
    def __init__(
        self,
        *,
        job_repo: IngestionJobRepository,
        graph_service: GraphService,
        enqueue: EnqueueFn,
    ) -> None:
        self._jobs = job_repo
        self._graphs = graph_service
        self._enqueue = enqueue

    async def submit(
        self,
        *,
        user_id: uuid.UUID,
        graph_id: uuid.UUID,
        data: bytes,
        filename: str | None,
        source_type: str,
        recipe_id: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        event_time: str | None = None,
    ) -> IngestionJobRecord:
        # owner gate (raises GraphNotFound -> 404 at the route) before any write
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)
        source_content = base64.b64encode(data).decode("ascii")
        job = await self._jobs.create(
            graph_id=graph_id,
            source_type=source_type,
            filename=filename,
            source_content=source_content,
            recipe_id=recipe_id,
            valid_from=valid_from,
            valid_to=valid_to,
            event_time=event_time,
        )
        # Durably COMMIT the job row before enqueueing: the request-scoped unit of work otherwise
        # commits only after the route returns, so the worker (a SEPARATE session) could pick up
        # the task before the row is visible and silently drop it as 'missing' (#267). Commit first,
        # so a fresh worker session is guaranteed to see the row before the task runs.
        await self._jobs.commit()
        self._enqueue(str(job.id), enforced_organisation_id())
        return job

    async def get_job(
        self, *, user_id: uuid.UUID, graph_id: uuid.UUID, job_id: uuid.UUID
    ) -> IngestionJobRecord:
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)
        job = await self._jobs.get(job_id)
        if job is None or job.graph_id != graph_id:
            raise JobNotFound(str(job_id))
        return job

    async def list_documents(
        self, *, user_id: uuid.UUID, graph_id: uuid.UUID
    ) -> list[IngestionJobRecord]:
        # /documents lists INGESTED documents. A community-detect job reuses the ingestion_jobs
        # table (no separate table) but is not a document, so it is excluded here — otherwise detect
        # jobs would surface as phantom documents in the list.
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)
        return await self._jobs.list_for_graph(
            graph_id, exclude_source_types=(COMMUNITY_DETECT_SOURCE_TYPE,)
        )

    async def list_artifacts(
        self,
        *,
        user_id: uuid.UUID,
        graph_id: uuid.UUID,
        q: str | None = None,
        source_type: str | None = None,
    ) -> list[IngestionJobRecord]:
        """The graph's ARTIFACTS (its ingested documents) for the unified /v1/artifacts surface
        (#543) — optionally filtered by a filename query ``q`` or ``source_type``. Org-scoped via
        graph ownership (a non-owned graph → GraphNotFound → 404). Verbatim content is served only
        by ``get_artifact`` (the list is summaries)."""
        records = await self.list_documents(user_id=user_id, graph_id=graph_id)
        if source_type:
            records = [r for r in records if r.source_type == source_type]
        if q:
            ql = q.lower()
            records = [r for r in records if ql in (r.filename or "").lower()]
        return records

    async def get_artifact(
        self, *, user_id: uuid.UUID, artifact_id: uuid.UUID
    ) -> tuple[IngestionJobRecord, str | None]:
        """One artifact's record + its verbatim content (#543). Org-scoped: the repo read is
        org-bound, and the owning graph is ownership-checked; a missing / cross-org artifact raises
        JobNotFound (→ 404)."""
        job = await self._jobs.get(artifact_id)
        if job is None:
            raise JobNotFound(str(artifact_id))
        await self._graphs.get_graph(graph_id=job.graph_id, user_id=user_id)
        content = await self._jobs.get_source_content(artifact_id)
        return job, content
