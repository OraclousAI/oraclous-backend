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
from oraclous_harness_runtime_service.core.rls import (
    RlsBypassingRoleError,
    assert_runtime_role_isolates,
)
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore
from oraclous_harness_runtime_service.repositories.assignment_repository import AssignmentRepository
from oraclous_harness_runtime_service.repositories.checkpoint_repository import CheckpointRepository
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.repositories.provenance_sink import PostgresProvenanceSink
from oraclous_harness_runtime_service.services.memory_client import drain_pending_writes

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

    # ADR-030 §3: fail closed LOUDLY if the runtime role bypasses RLS (a superuser / BYPASSRLS role
    # makes the FORCE'd policy inert — T1-M3). Distinct from the Postgres-unavailable degrade above:
    # a mis-deployed bypassing role is a hard configuration error, so it exits the process rather
    # than quietly serving an unscoped store. Gated on HARNESS_RLS_ASSERT_RUNTIME_ROLE (the deployed
    # app runtime sets it; a deliberate owner-DSN dev/test run leaves it off). Asserts against the
    # execution repo's engine — all four harness repos (execution/checkpoint/assignment/provenance)
    # build on the same DSN/role, so one proves the role. The harness has no Celery/background
    # worker (all DB access is in-request through these four repos), so this web-startup check is
    # the only role assertion needed — there is no out-of-request worker engine to guard.
    if settings.rls_assert_runtime_role and execution_repo is not None:
        try:
            await assert_runtime_role_isolates(execution_repo.engine)
        except RlsBypassingRoleError as exc:
            alert(
                Severity.ERROR,
                "rls_runtime_role_bypasses",
                "harness-runtime-service",
                "runtime DB role bypasses RLS; refusing to start (ADR-030 §3)",
                error=str(exc),
            )
            raise SystemExit(1) from exc

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
        # Post-run memory hook (#332 / ADR-027 §5): give any in-flight fire-and-forget writes a
        # SHORT bounded grace to land before teardown cancels them. Fail-soft + bounded — it never
        # raises and never delays shutdown beyond memory_drain_timeout (a no-op when nothing is in
        # flight or the flag is off).
        if settings.memory_writes:
            await drain_pending_writes(timeout=settings.memory_drain_timeout)
        if execution_repo is not None:
            await execution_repo.close()
        if assignment_repo is not None:
            await assignment_repo.close()
        if checkpoint_repo is not None:
            await checkpoint_repo.close()
        if sink is not None:
            await sink.close()
