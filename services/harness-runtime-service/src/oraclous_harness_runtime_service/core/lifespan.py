"""App lifecycle (ORAA-4 §21 core layer) — open/close the Postgres store + provenance sink.

Schema is created by the Alembic one-shot. Degrades gracefully: if Postgres is unreachable
at startup the app still serves ``/health`` and the execute/read routes report 503.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from oraclous_substrate import ProvenanceCollector
from oraclous_telemetry import Severity, alert, evaluate_readiness, exit_on_degrade_enabled

from oraclous_harness_runtime_service.core.config import get_settings
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore
from oraclous_harness_runtime_service.repositories.assignment_repository import AssignmentRepository
from oraclous_harness_runtime_service.repositories.checkpoint_repository import CheckpointRepository
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.repositories.provenance_sink import PostgresProvenanceSink

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    execution_repo: ExecutionRepository | None = None
    assignment_repo: AssignmentRepository | None = None
    checkpoint_repo: CheckpointRepository | None = None
    sink: PostgresProvenanceSink | None = None
    try:
        execution_repo = ExecutionRepository(settings.database_url)
        assignment_repo = AssignmentRepository(settings.database_url)
        checkpoint_repo = CheckpointRepository(settings.database_url)
        sink = PostgresProvenanceSink(settings.database_url)
        app.state.execution_repository = execution_repo
        app.state.assignment_repository = assignment_repo
        app.state.checkpoint_repository = checkpoint_repo
        app.state.provenance = ProvenanceCollector(sink)
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health reflects it
        app.state.execution_repository = None
        app.state.assignment_repository = None
        app.state.checkpoint_repository = None
        app.state.provenance = None
        alert(
            Severity.ERROR,
            "store_bind_failed",
            "harness-runtime-service",
            "Postgres unavailable at startup; execute/read disabled",
            store="postgres",
            error=str(exc),
        )

    verdict = evaluate_readiness({"postgres": app.state.execution_repository})
    if verdict.is_degraded and exit_on_degrade_enabled():
        raise SystemExit(1)

    # Fail-closed LLM-mode default is `live` (ADR-021 §1). Selecting the scripted fake responder is
    # valid for CI/smoke but must be EXPLICIT — fire a loud one-time startup alert here so a deploy
    # running the scripted LLM by accident is impossible to miss (never a buried WARNING).
    if settings.llm_mode == "fake":
        alert(
            Severity.WARNING,
            "fake_runtime_active",
            "harness-runtime-service",
            "HARNESS_LLM_MODE=fake: the agent loop uses the SCRIPTED key-free LLM responder — "
            "valid for dev/CI/smoke only; a real deploy must unset this (ADR-021 §1)",
            surface="llm",
        )

    # OHM signature trust store (config-driven). A malformed key degrades to an empty store so
    # /health still serves; verification then fail-closes on any signed OHM (unknown signer).
    try:
        app.state.trust_store = TrustStore(settings.ohm_trust_keys)
    except Exception as exc:  # noqa: BLE001 — degrade: empty trust store, fail-closed on signed OHMs
        logger.warning("OHM trust store failed to load; treating as empty: %s", exc)
        app.state.trust_store = TrustStore({})

    try:
        yield
    finally:
        if execution_repo is not None:
            await execution_repo.close()
        if assignment_repo is not None:
            await assignment_repo.close()
        if checkpoint_repo is not None:
            await checkpoint_repo.close()
        if sink is not None:
            await sink.close()
