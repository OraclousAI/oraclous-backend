import enum

from app.models.base import Base, TimestampMixin, UUIDMixin
from sqlalchemy import Column, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID


class DescriptorKind(enum.StrEnum):
    TOOL = "tool"
    SKILL = "skill"
    AGENT = "agent"
    HARNESS = "harness"
    HUMAN_ROLE = "human_role"


class CapabilityDescriptorDB(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "capability_descriptor"

    org_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    kind = Column(
        SAEnum(
            DescriptorKind,
            name="descriptorkind",
            create_type=False,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )
    content_hash = Column(String(255), nullable=True)
    descriptor = Column(JSONB, nullable=False)
