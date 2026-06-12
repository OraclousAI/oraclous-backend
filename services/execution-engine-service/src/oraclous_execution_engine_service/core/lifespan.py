"""App lifecycle (ORAA-4 §21 core layer) — open/close the Postgres store + provenance sink.

Schema is created by the Alembic one-shot. Degrades gracefully: if Postgres is unreachable at
startup the app still serves ``/health`` and the job routes report 503.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from oraclous_substrate import ProvenanceCollector
from oraclous_telemetry import Severity, alert, evaluate_readiness, exit_on_degrade_enabled

from oraclous_execution_engine_service.core.config import get_settings
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.provenance_repository import (
    ProvenanceRepository,
)
from oraclous_execution_engine_service.repositories.provenance_sink import PostgresProvenanceSink
from oraclous_execution_engine_service.repositories.roundtable_repository import (
    RoundtableRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    job_repo: JobRepository | None = None
    schedule_repo: ScheduleRepository | None = None
    roundtable_repo: RoundtableRepository | None = None
    provenance_repo: ProvenanceRepository | None = None
    sink: PostgresProvenanceSink | None = None
    try:
        job_repo = JobRepository(settings.database_url)
        schedule_repo = ScheduleRepository(settings.database_url)
        roundtable_repo = RoundtableRepository(settings.database_url)
        provenance_repo = ProvenanceRepository(settings.database_url)
        sink = PostgresProvenanceSink(settings.database_url)
        app.state.job_repository = job_repo
        app.state.schedule_repository = schedule_repo
        app.state.roundtable_repository = roundtable_repo
        app.state.provenance_repository = provenance_repo
        app.state.provenance = ProvenanceCollector(sink)
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health reflects it
        app.state.job_repository = None
        app.state.schedule_repository = None
        app.state.roundtable_repository = None
        app.state.provenance_repository = None
        app.state.provenance = None
        alert(
            Severity.ERROR,
            "store_bind_failed",
            "execution-engine-service",
            "Postgres unavailable at startup; job routes disabled",
            store="postgres",
            error=str(exc),
        )

    verdict = evaluate_readiness({"postgres": app.state.job_repository})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

    try:
        yield
    finally:
        if job_repo is not None:
            await job_repo.close()
        if schedule_repo is not None:
            await schedule_repo.close()
        if roundtable_repo is not None:
            await roundtable_repo.close()
        if provenance_repo is not None:
            await provenance_repo.close()
        if sink is not None:
            await sink.close()
