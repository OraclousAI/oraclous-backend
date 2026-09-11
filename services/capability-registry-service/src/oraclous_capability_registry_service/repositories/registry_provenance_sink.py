"""Postgres provenance sink (repositories layer; CLAUDE.md §3.7).

The concrete ``ProvenanceSink`` behind the substrate ``ProvenanceCollector`` for this service. It is
the only place that persists ``registry_provenance`` rows (no direct-to-store bypass elsewhere,
CLAUDE.md §3.7). Strictly org-isolated (ADR-030 §1, ADR-006): every write binds the record's own
``organisation_id`` via ``org_scope`` so the FORCE'd RLS ``WITH CHECK`` admits it under the
``oraclous_app`` runtime role.
"""

from __future__ import annotations

import uuid

from oraclous_substrate import ProvenanceRecord, ProvenanceSink
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_capability_registry_service.core.rls import build_rls_engine, org_scope
from oraclous_capability_registry_service.models.registry_provenance import RegistryProvenance


class PostgresProvenanceSink(ProvenanceSink):
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def write(self, record: ProvenanceRecord) -> None:
        organisation_id = uuid.UUID(record.organisation_id)
        row = RegistryProvenance(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            principal=record.principal,
            action=record.action,
            resource=record.resource,
            outcome=record.outcome,
            context=dict(record.context) if record.context is not None else None,
            input_hash=record.input_hash,
            output_hash=record.output_hash,
        )
        # ADR-030: bind the record's own org so the FORCE'd RLS WITH CHECK admits the INSERT.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
