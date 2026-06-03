import uuid

from app.models.base import Base, TimestampMixin
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import UUID


class HarnessCapabilityAllocationDB(Base, TimestampMixin):
    __tablename__ = "harness_capability_allocations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    harness_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    capability_id = Column(UUID(as_uuid=True), nullable=False)
