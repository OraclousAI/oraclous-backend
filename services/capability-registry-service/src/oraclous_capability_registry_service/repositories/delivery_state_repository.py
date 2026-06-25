"""Deliver-back delivery_state repository (repositories layer; #515, O7).

The ONLY place that touches the DB driver for delivery_state. Every read/write is scoped by
``organisation_id`` (ADR-006, supplied from the authenticated principal, never a body — ORG001) and
runs under the registry RLS backstop (ADR-030 begin-guard binds the org-GUC per tx). ``get_hashes``
returns the last-written per-file hashes for a target so the connector computes the minimal diff;
``record`` dedupes an identical re-deliver (returns False) and upserts only the changed files.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_capability_registry_service.core.rls import build_rls_engine, org_scope
from oraclous_capability_registry_service.models.delivery_state import DeliveryState


class DeliveryStateRepository:
    def __init__(self, db_url: str) -> None:
        # ADR-030: RLS org-GUC begin-guard installed on the engine (every tx binds the org).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def get_hashes(
        self, *, organisation_id: uuid.UUID, repo: str, ref: str
    ) -> dict[str, str]:
        """The last-written ``{path: content_hash}`` for this org's (repo, ref) — ``{}`` for another
        org (RLS + the app-layer org predicate)."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(DeliveryState.path, DeliveryState.content_hash).where(
                        DeliveryState.organisation_id == organisation_id,
                        DeliveryState.repo == repo,
                        DeliveryState.ref == ref,
                    )
                )
                return {path: content_hash for path, content_hash in result.all()}

    async def record(
        self,
        *,
        organisation_id: uuid.UUID,
        repo: str,
        ref: str,
        file_hashes: dict[str, str],
        delivery_key: str | None = None,
    ) -> bool:
        """Record the delivered file hashes. Returns False (a NO_OP) when ``delivery_key`` was
        already recorded for this org (an identical re-deliver); else upserts each ``(path, hash)``
        (so a re-record updates only the changed file) and returns True. Org-stamped, not a body."""
        with org_scope(organisation_id):
            async with self._session() as session, session.begin():
                if delivery_key is not None:
                    seen = await session.execute(
                        select(DeliveryState.id)
                        .where(
                            DeliveryState.organisation_id == organisation_id,
                            DeliveryState.delivery_key == delivery_key,
                        )
                        .limit(1)
                    )
                    if seen.first() is not None:
                        return False  # this org already delivered this exact content set → NO_OP
                key = delivery_key or ""
                for path, content_hash in file_hashes.items():
                    await session.execute(
                        pg_insert(DeliveryState)
                        .values(
                            id=uuid.uuid4(),
                            organisation_id=organisation_id,
                            repo=repo,
                            ref=ref,
                            path=path,
                            content_hash=content_hash,
                            delivery_key=key,
                        )
                        .on_conflict_do_update(
                            index_elements=["organisation_id", "repo", "ref", "path"],
                            set_={"content_hash": content_hash, "delivery_key": key},
                        )
                    )
                return True
