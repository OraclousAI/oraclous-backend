"""UserCredential storage model (reshape of legacy ``app/models/credential_model.py``).

Adds ``organisation_id`` as the outermost (NOT NULL) tenancy scope above the
existing ``user_id``, per ADR-006 and the ORG002 storage-model guardrail.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, Enum, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from oraclous_credential_broker_service.models.base_model import BaseModel
from oraclous_credential_broker_service.models.enums import CredentialType


class UserCredential(BaseModel):
    __tablename__ = "user_credentials"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(PG_UUID(as_uuid=True), nullable=False)
    name = Column(String, nullable=True)
    provider = Column(String, nullable=False)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False)
    tool_id = Column(PG_UUID(as_uuid=True), nullable=False)
    encrypted_cred = Column(String, nullable=False)
    cred_type = Column(Enum(CredentialType, native_enum=False), nullable=True)
