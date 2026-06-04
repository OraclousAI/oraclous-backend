"""Auth audit repository (ORAA-4 §21 repositories layer — the only ``auth_audit_log`` SQL).

Append-only: ``record`` inserts one immutable event row. Auditing is best-effort and must not break
the audited operation, so callers fold failures into a no-op (see the services).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_auth_service.models.audit_model import AuthAuditLog


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        event: str,
        actor_type: str,
        actor_id: str | None = None,
        organisation_id: str | None = None,
        target: str | None = None,
        event_metadata: dict | None = None,
    ) -> None:
        self._session.add(
            AuthAuditLog(
                id=str(uuid.uuid4()),
                organisation_id=organisation_id,
                actor_id=actor_id,
                actor_type=actor_type,
                event=event,
                target=target,
                event_metadata=event_metadata,
            )
        )
        await self._session.flush()
