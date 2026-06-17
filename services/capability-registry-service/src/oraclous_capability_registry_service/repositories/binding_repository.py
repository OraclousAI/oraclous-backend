"""Harness↔graph binding repository (ORAA-4 §21 repositories layer; ADR-029).

The ONLY place that touches the DB driver for harness↔graph bindings. Every read and write is scoped
by ``organisation_id`` (ADR-006) — supplied by the caller from the authenticated principal, never a
request body (ORG001).

``attach`` is idempotent: the ``UNIQUE(harness_capability_id, graph_id)`` constraint means a
duplicate insert raises ``IntegrityError``, which is caught and reported as an already-bound success
(``created=False``) — so a re-attach is a 200, never a 409 (ADR-029 §6). ``detach`` is org-scoped
(a row in another org is invisible → not deleted → the service 404s). The read paths return the
binding rows; filtering dangling ``graph_id``s against the live-graph set (ADR-029 §4) is the
service's job (this layer has no cross-service reach).
"""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_capability_registry_service.core.rls import build_rls_engine, org_scope
from oraclous_capability_registry_service.models.harness_graph_binding import HarnessGraphBinding


class BindingRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def attach(
        self,
        *,
        organisation_id: uuid.UUID,
        harness_capability_id: uuid.UUID,
        graph_id: uuid.UUID,
        created_by: uuid.UUID,
    ) -> tuple[HarnessGraphBinding, bool]:
        """Bind a harness to a graph. Returns ``(row, created)``.

        ``created`` is True for a fresh row, False when the pair already exists (idempotent — the
        unique-constraint ``IntegrityError`` is mapped to a re-fetch of the existing row). The org
        is stamped from the caller, never the body (ORG001).
        """
        row = HarnessGraphBinding(
            id=uuid.uuid4(),
            harness_capability_id=harness_capability_id,
            graph_id=graph_id,
            organisation_id=organisation_id,
            created_by=created_by,
        )
        # ADR-030: bind the caller's org so the engine begin-guard sets app.current_organisation_id;
        # without it the FORCE'd RLS WITH CHECK denies the INSERT (42501) under oraclous_app.
        # The idempotent re-fetch (_get_pair) stays inside the same scope so its read sees the GUC.
        with org_scope(organisation_id):
            async with self._session() as session:
                try:
                    async with session.begin():
                        session.add(row)
                except IntegrityError:
                    existing = await self._get_pair(
                        session,
                        harness_capability_id=harness_capability_id,
                        graph_id=graph_id,
                        organisation_id=organisation_id,
                    )
                    if existing is not None:
                        return existing, False
                    raise
                await session.refresh(row)
                return row, True

    async def _get_pair(
        self,
        session: object,
        *,
        harness_capability_id: uuid.UUID,
        graph_id: uuid.UUID,
        organisation_id: uuid.UUID,
    ) -> HarnessGraphBinding | None:
        result = await session.execute(  # type: ignore[attr-defined]
            select(HarnessGraphBinding).where(
                HarnessGraphBinding.harness_capability_id == harness_capability_id,
                HarnessGraphBinding.graph_id == graph_id,
                HarnessGraphBinding.organisation_id == organisation_id,
            )
        )
        return result.scalars().first()

    async def detach(
        self,
        *,
        organisation_id: uuid.UUID,
        harness_capability_id: uuid.UUID,
        graph_id: uuid.UUID,
    ) -> bool:
        """Remove the binding for the caller's org. Returns True iff a row was deleted (a missing /
        cross-org pair returns False → the service 404s)."""
        # ADR-030: bind the caller's org so RLS scopes the delete to this org (else the empty GUC →
        # zero rows match → False). The app-layer organisation_id predicate stays defense-in-depth.
        with org_scope(organisation_id):
            async with self._session() as session, session.begin():
                result = await session.execute(
                    delete(HarnessGraphBinding).where(
                        HarnessGraphBinding.harness_capability_id == harness_capability_id,
                        HarnessGraphBinding.graph_id == graph_id,
                        HarnessGraphBinding.organisation_id == organisation_id,
                    )
                )
                return (cast("CursorResult[object]", result).rowcount or 0) > 0

    async def list_by_graph(
        self, *, organisation_id: uuid.UUID, graph_id: uuid.UUID
    ) -> list[HarnessGraphBinding]:
        """The bindings for one graph in the caller's org (oldest first)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(HarnessGraphBinding)
                    .where(
                        HarnessGraphBinding.graph_id == graph_id,
                        HarnessGraphBinding.organisation_id == organisation_id,
                    )
                    .order_by(HarnessGraphBinding.created_at)
                )
                return list(result.scalars().all())

    async def list_by_harness(
        self, *, organisation_id: uuid.UUID, harness_capability_id: uuid.UUID
    ) -> list[HarnessGraphBinding]:
        """The bindings for one harness in the caller's org (oldest first)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(HarnessGraphBinding)
                    .where(
                        HarnessGraphBinding.harness_capability_id == harness_capability_id,
                        HarnessGraphBinding.organisation_id == organisation_id,
                    )
                    .order_by(HarnessGraphBinding.created_at)
                )
                return list(result.scalars().all())
