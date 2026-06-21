"""App lifecycle (core layer) — open/close the Postgres store + provenance sink.

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
from oraclous_execution_engine_service.core.rls import (
    RlsBypassingRoleError,
    assert_runtime_role_isolates,
    build_rls_engine,
)
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.provenance_repository import (
    ProvenanceRepository,
)
from oraclous_execution_engine_service.repositories.provenance_sink import PostgresProvenanceSink
from oraclous_execution_engine_service.repositories.roundtable_repository import (
    RoundtableRepository,
)
from oraclous_execution_engine_service.repositories.schedule_repository import ScheduleRepository
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # ADR-030 §3: fail closed LOUDLY if the ORG-BOUND runtime role bypasses RLS (a superuser /
    # BYPASSRLS role makes the FORCE'd policy inert — T1-M3). A mis-deployed bypassing role is a
    # hard configuration error, so it exits the process rather than quietly serving an unscoped
    # store. Gated on ENGINE_RLS_ASSERT_RUNTIME_ROLE (the deployed oraclous_app api + worker set it;
    # a deliberate owner-DSN dev/test run leaves it off). Asserts the org-bound DSN the request path
    # uses — the maintenance/owner engine is intended to bypass RLS for the cross-org sweeps and is
    # not asserted.
    if settings.rls_assert_runtime_role:
        assert_engine = build_rls_engine(settings.database_url)
        try:
            await assert_runtime_role_isolates(assert_engine)
        except RlsBypassingRoleError as exc:
            alert(
                Severity.ERROR,
                "rls_runtime_role_bypasses",
                "execution-engine-service",
                "runtime DB role bypasses RLS; refusing to start (ADR-030 §3)",
                error=str(exc),
            )
            await assert_engine.dispose()
            raise SystemExit(1) from exc
        await assert_engine.dispose()

    job_repo: JobRepository | None = None
    schedule_repo: ScheduleRepository | None = None
    roundtable_repo: RoundtableRepository | None = None
    team_run_repo: TeamRunRepository | None = None
    provenance_repo: ProvenanceRepository | None = None
    sink: PostgresProvenanceSink | None = None
    try:
        job_repo = JobRepository(settings.database_url)
        schedule_repo = ScheduleRepository(settings.database_url)
        roundtable_repo = RoundtableRepository(settings.database_url)
        team_run_repo = TeamRunRepository(settings.database_url)
        provenance_repo = ProvenanceRepository(settings.database_url)
        sink = PostgresProvenanceSink(settings.database_url)
        app.state.job_repository = job_repo
        app.state.schedule_repository = schedule_repo
        app.state.roundtable_repository = roundtable_repo
        app.state.team_run_repository = team_run_repo
        app.state.provenance_repository = provenance_repo
        app.state.provenance = ProvenanceCollector(sink)
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health reflects it
        app.state.job_repository = None
        app.state.schedule_repository = None
        app.state.roundtable_repository = None
        app.state.team_run_repository = None
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
        if team_run_repo is not None:
            await team_run_repo.close()
        if provenance_repo is not None:
            await provenance_repo.close()
        if sink is not None:
            await sink.close()
