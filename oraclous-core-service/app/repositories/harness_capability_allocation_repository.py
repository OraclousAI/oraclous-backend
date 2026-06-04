import uuid

from app.models.harness_capability_allocation import HarnessCapabilityAllocationDB
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class HarnessCapabilityAllocationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        org_id: uuid.UUID,
        harness_id: uuid.UUID,
        capability_id: uuid.UUID,
    ) -> HarnessCapabilityAllocationDB:
        row = HarnessCapabilityAllocationDB(
            org_id=org_id,
            harness_id=harness_id,
            capability_id=capability_id,
        )
        self.db.add(row)
        await self.db.flush()
        await self.db.refresh(row)
        return row

    async def list_by_harness(
        self,
        org_id: uuid.UUID,
        harness_id: uuid.UUID,
    ) -> list[HarnessCapabilityAllocationDB]:
        result = await self.db.execute(
            select(HarnessCapabilityAllocationDB).where(
                HarnessCapabilityAllocationDB.org_id == org_id,
                HarnessCapabilityAllocationDB.harness_id == harness_id,
            )
        )
        return list(result.scalars().all())
