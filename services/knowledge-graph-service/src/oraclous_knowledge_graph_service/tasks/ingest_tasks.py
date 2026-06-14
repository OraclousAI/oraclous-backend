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
import logging
import uuid
from typing import Any

from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_substrate.access import enforced_organisation_id

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.core.database import make_sessionmaker, make_worker_engine
from oraclous_knowledge_graph_service.core.neo4j import make_neo4j_driver
from oraclous_knowledge_graph_service.core.redis import make_redis_lock_client
from oraclous_knowledge_graph_service.domain.extraction_schema import (
    to_graph_schema,
    to_prompt_prefix,
)
from oraclous_knowledge_graph_service.domain.ontology import Ontology
from oraclous_knowledge_graph_service.repositories.graph_generation_repository import (
    GraphGenerationRepository,
)
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
from oraclous_knowledge_graph_service.tasks.code_stale_tasks import cleanup_stale_code_task


class JobNotVisibleYet(Exception):
    """The job row is not yet visible to the worker's session — a read-after-write race (#267).

    Raised (rather than silently returning 'missing') so Celery RETRIES the task with bounded
    backoff: any residual race between the submit commit and the worker pickup self-heals instead
    of silently dropping the submission. After the bounded retries are exhausted the task fails
    loudly (the row genuinely does not exist), never reporting a false success.
    """


@celery_app.task(
    bind=True,
    name="kgs.ingest_document",
    autoretry_for=(JobNotVisibleYet,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    max_retries=5,
)
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
                    # The row isn't visible to this fresh worker session yet — most likely a
                    # read-after-write race against the submit commit (#267). Raise so Celery
                    # retries with backoff instead of returning a silent 'success'; the submission
                    # self-heals rather than being dropped.
                    raise JobNotVisibleYet(job_id_s)
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
                    # Slice B: a graph's TYPED ontology drives free-text extraction — compile it to
                    # a hard GraphSchema (the extractor enforces it) + a prompt prefix (soft
                    # steering), and pass the ontology through for the strict/coerce post-pass.
                    async with maker() as session:
                        ontology_data = await GraphRepository(session).get_ontology(
                            payload.graph_id
                        )
                    ontology = Ontology.of(ontology_data)
                    extractor = make_extractor(
                        settings,
                        schema=to_graph_schema(ontology),
                        prompt_prefix=to_prompt_prefix(ontology),
                    )
                    write_repo = GraphWriteRepository(driver, database=settings.neo4j_database)
                    ingestion = IngestionService(
                        write_repo, make_embedder(settings), extractor, ontology=ontology
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
                            "ontology_violations": result.ontology_violations,
                            "ontology_coercions": result.ontology_coercions,
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
            # Bump the per-graph generation (#308): the graph just changed, so the retriever's
            # cached reads for prior generations become a natural cache-miss. A neutral version
            # signal — the KGS never touches the retriever's private cache keys. The Redis driver is
            # built inside the repository (the §21 driver layer), not here. Advisory: a Redis outage
            # is swallowed (the cache then TTL-expires), never failing a completed ingest. Off the
            # event loop, as the bump opens a sync client.
            await asyncio.to_thread(
                GraphGenerationRepository.bump_for,
                redis_url=settings.redis_url,
                organisation_id=organisation_id_s,
                graph_id=str(payload.graph_id),
            )
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
            # Only a promoted recipe is runnable — a draft must be promoted first (ADR-028), so a
            # run always pins to a reviewed, immutable recipe version.
            if recipe.get("status") != "promoted":
                raise RuntimeError(
                    f"recipe {payload.recipe_id!r} is a draft; promote it before ingesting"
                )
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
        settings=settings,
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
    # `entities_extracted` (hybrid free-text-on-a-field, Slice 2; already the post-resolution count
    # of canonical entity nodes when Slice 4 resolution is on) is folded into the headline entity
    # count; `mentions` (the MENTIONS edge per entity) + `similarity_edges` (SIMILAR_TO edges per
    # similar pair, Slice 3) + `resolution_candidates` (the SAME_AS_CANDIDATE review edges, Slice 4)
    # are folded into relationships. `entities_merged` (Slice 4) counts surface variants folded onto
    # a representative — nodes NOT created — so it is surfaced in `detail`, not added to the totals.
    return {
        "entities": result["nodes_written"]
        + result["containers_written"]
        + result.get("entities_extracted", 0),
        "relationships": result["edges_written"]
        + result.get("mentions", 0)
        + result.get("similarity_edges", 0)
        + result.get("resolution_candidates", 0),
        "detail": result,
    }


async def _ingest_code(*, driver, settings, payload, data: bytes) -> dict[str, Any]:
    """Code (zip / single file): the full 6-stage pipeline via the org-scoped code writer.

    bootstrap (deps) -> delta (SHA, stale-mark changed + deleted) -> AST parse -> cross-file resolve
    -> embeddings (fail-soft) -> write, all under the per-(org,graph) advisory lock (#305) so two
    re-ingests of the same graph serialise (no mark→revive strand) and a concurrent Stage-6 sweep
    can't reap a node this ingest is reviving. After a re-ingest that marked anything stale (changed
    OR deleted files), enqueue the Stage-6 sweep (TTL-gated, so just-marked symbols survive the
    grace window)."""
    # make_redis_lock_client does a blocking ping(); run it off the event loop (mirrors the
    # community path) so a slow/unreachable Redis can't block the worker's event loop. Advisory:
    # None (Redis down) degrades to lock-off — the ingest still runs.
    lock_client = await asyncio.to_thread(make_redis_lock_client, settings)
    try:
        service = CodeIngestionService(
            driver=driver,
            organisation_id=enforced_organisation_id(),
            database=settings.neo4j_database,
            settings=settings,
            lock_client=lock_client,
        )
        counts = await asyncio.to_thread(
            service.ingest,
            graph_id=str(payload.graph_id),
            document=payload.filename or "code.zip",
            data=data,
        )
    finally:
        if lock_client is not None:
            try:
                await asyncio.to_thread(lock_client.close)
            except Exception as exc:  # noqa: BLE001 — best-effort close of the advisory lock client
                logging.getLogger(__name__).debug("code-ingest lock client close skipped: %s", exc)
    if counts.get("files_changed") or counts.get("files_deleted"):
        # Decouple the sweep from the request: enqueue (never block the ingest on cleanup).
        cleanup_stale_code_task.delay(str(payload.graph_id), enforced_organisation_id())
    entities = counts["files"] + counts["functions"] + counts["classes"] + counts["variables"]
    relationships = counts["calls"] + counts["imports"] + counts["inherits"]
    return {"entities": entities, "relationships": relationships, "detail": counts}
