"""Ingestion-job use-cases (services layer).

Two gates, both reused from GraphService (#736 / ADR-051): a job is READABLE on any graph in the
caller's organisation (the read gate — documents, artifacts, artifact content, job status) and
CREATABLE only on a graph the caller owns (the owner gate — `submit`). Cross-org → 404 either way,
and the repository enforces the org underneath both. `submit`
creates the job row, COMMITS it (so the worker's separate session can see it — see #267), then
enqueues the Celery task, passing organisation_id explicitly so the worker can re-bind the org
context across the process boundary. `enqueue` is injected so the service stays testable without a
broker.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from collections.abc import Callable

from oraclous_citation import SourceRef
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.domain.artifact_naming import ProducerRef
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
        producer: ProducerRef | None = None,
        source: SourceRef | None = None,
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
            # #728: an agent artifact carries its producer; a user upload passes none and keeps
            # NULL, which is what preserves filename-based document keying (#522).
            producer=producer,
            content_hash=hashlib.sha256(data).hexdigest(),
            # Stored verbatim: the connector is the only party that may state a source, and the
            # citation is minted in the worker, which cannot see the request body.
            source=source.model_dump(mode="json") if source is not None else None,
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
        await self._graphs.assert_readable(graph_id=graph_id)  # org read gate (#736)
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
        # ADR-051: listing a graph's documents is a READ, so it is org-scoped — a member no longer
        # gets a 404 on a graph they can already search.
        await self._graphs.assert_readable(graph_id=graph_id)
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
        team_run_id: uuid.UUID | None = None,
        team_id: str | None = None,
        member_role: str | None = None,
        producer_kind: str | None = None,
    ) -> list[IngestionJobRecord]:
        """The graph's ARTIFACTS (its ingested documents) for the unified /v1/artifacts surface
        (#543) — optionally filtered by a filename query ``q`` or ``source_type``. Org-scoped via
        the read gate (#736 / ADR-051): a graph outside the caller's organisation → GraphNotFound →
        404. Verbatim content is served only by ``get_artifact`` (the list is summaries).

        #728: the provenance filters answer "what did this run produce", "what has this team ever
        produced" and "what did this member write" — the questions an artifact could not be asked
        while it recorded nothing about who made it."""
        await self._graphs.assert_readable(graph_id=graph_id)  # org read gate → 404 (#736)
        records = await self._jobs.list_for_graph(
            graph_id,
            exclude_source_types=(COMMUNITY_DETECT_SOURCE_TYPE,),
            team_run_id=team_run_id,
            team_id=team_id,
            member_role=member_role,
            producer_kind=producer_kind,
        )
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
        org-bound, and the owning graph passes the read gate (#736 / ADR-051); a missing artifact,
        or one whose graph is outside the caller's organisation, raises JobNotFound / GraphNotFound
        (→ 404)."""
        job = await self._jobs.get(artifact_id)
        if job is None:
            raise JobNotFound(str(artifact_id))
        await self._graphs.assert_readable(graph_id=job.graph_id)  # org read gate (#736)
        raw = await self._jobs.get_source_content(artifact_id)
        # ingest stores source_content base64-encoded (submit, above; the worker decodes it the same
        # way) — serve the VERBATIM file, decoded. Fall back to the raw value if it is not base64.
        content: str | None = raw
        if raw is not None:
            try:
                content = base64.b64decode(raw, validate=True).decode("utf-8", errors="replace")
            except (ValueError, binascii.Error):
                content = raw
        return job, content
