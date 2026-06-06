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

from oraclous_harness_runtime_service.core.config import get_settings
from oraclous_harness_runtime_service.domain.ohm.signatures import TrustStore
from oraclous_harness_runtime_service.repositories.execution_repository import ExecutionRepository
from oraclous_harness_runtime_service.repositories.provenance_sink import PostgresProvenanceSink

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    execution_repo: ExecutionRepository | None = None
    sink: PostgresProvenanceSink | None = None
    try:
        execution_repo = ExecutionRepository(settings.database_url)
        sink = PostgresProvenanceSink(settings.database_url)
        app.state.execution_repository = execution_repo
        app.state.provenance = ProvenanceCollector(sink)
    except Exception as exc:  # noqa: BLE001 — degrade: data routes 503, /health still serves
        logger.warning("Postgres unavailable at startup; execute/read disabled: %s", exc)
        app.state.execution_repository = None
        app.state.provenance = None

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
        if sink is not None:
            await sink.close()
