"""UserCredential storage model (reshape of legacy ``app/models/credential_model.py``).

Adds ``organisation_id`` as the outermost (NOT NULL) tenancy scope above the
existing ``user_id``, per ADR-006 and the ORG002 storage-model guardrail.

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid

from sqlalchemy import Enum, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_credential_broker_service.models.base_model import BaseModel
from oraclous_credential_broker_service.models.enums import CredentialType


class UserCredential(BaseModel):
    __tablename__ = "user_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tool_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    encrypted_cred: Mapped[str] = mapped_column(String, nullable=False)
    cred_type: Mapped[CredentialType | None] = mapped_column(
        Enum(CredentialType, native_enum=False), nullable=True
    )
