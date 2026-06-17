"""Postgres provenance sink (ORAA-4 §21 repositories layer; CLAUDE.md §3.7).

The concrete ``ProvenanceSink`` behind the substrate ``ProvenanceCollector``. It validates
the required-field contract and calls ``write`` exactly once per event; this sink is the only place
that persists provenance (no direct-to-store bypass elsewhere).
"""

from __future__ import annotations

import uuid

from oraclous_substrate import ProvenanceRecord, ProvenanceSink
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_harness_runtime_service.core.rls import build_rls_engine, org_scope
from oraclous_harness_runtime_service.models.provenance import HarnessProvenanceEvent


class PostgresProvenanceSink(ProvenanceSink):
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org). This is
        # the fourth independent harness repository engine; the INSERT-only write path below stamps
        # the record's organisation_id, which the FORCE'd RLS WITH CHECK admits only when it equals
        # the bound org (a cross-org provenance write raises 42501).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def write(self, record: ProvenanceRecord) -> None:
        organisation_id = uuid.UUID(record.organisation_id)
        row = HarnessProvenanceEvent(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            principal=record.principal,
            action=record.action,
            resource=record.resource,
            outcome=record.outcome,
        )
        # ADR-030: INSERT-ONLY path. Bind the record's org so the engine begin-guard sets the GUC;
        # the FORCE'd RLS WITH CHECK admits the provenance INSERT only when the stamped org equals
        # the bound one (a cross-org provenance write raises 42501). The org is the record's own
        # organisation_id (resolved upstream from authenticated context), never request input.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
