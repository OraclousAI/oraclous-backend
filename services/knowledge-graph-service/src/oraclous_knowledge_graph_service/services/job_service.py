"""Ingestion-job use-cases (ORAA-4 §21 services layer).

Owner-gated (a job is only visible/creatable on a graph the caller owns — reuses GraphService's
gate, so cross-user/cross-org → 404) and org-scoped (the repository enforces the org). `submit`
creates the job row then enqueues the Celery task, passing organisation_id explicitly so the worker
can re-bind the org context across the process boundary. `enqueue` is injected so the service stays
testable without a broker.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable

from oraclous_substrate.access import enforced_organisation_id

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
        await self._graphs.get_graph(graph_id=graph_id, user_id=user_id)
        return await self._jobs.list_for_graph(graph_id)
