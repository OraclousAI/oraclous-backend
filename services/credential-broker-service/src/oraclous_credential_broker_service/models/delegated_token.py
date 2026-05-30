"""``DelegatedToken`` storage model (ORA-32 / R1-B1).

The broker persists a row per minted delegated token bound to
``(organisation, member, agent, scopes, expiry)``. The raw bearer bytes are
**not** stored — only a SHA-256 hash and a lookup prefix — so AC4 (tokens are
internal-only) holds even if the row is exfiltrated.

ADR-006: ``organisation_id`` is the outermost tenancy scope (NOT NULL UUID),
stamped from the authenticated caller's context.

Reshape of the legacy ``credential-broker-service/app/models/credential_model.py``
storage-row idiom (UUID pk, ``organisation_id`` outermost, ORM via the shared
``BaseModel``). The delegated-token primitive itself is new — the legacy code
had no precursor — so this is a Reshape of the idiom, not of behaviour.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from oraclous_credential_broker_service.models.base_model import BaseModel


class DelegatedToken(BaseModel):
    __tablename__ = "delegated_tokens"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(PG_UUID(as_uuid=True), nullable=False)
    member_id = Column(PG_UUID(as_uuid=True), nullable=False)
    agent_id = Column(PG_UUID(as_uuid=True), nullable=False)
    scopes = Column(ARRAY(String), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String, nullable=False, default="active")
    token_hash = Column(String, nullable=False)
    token_prefix = Column(String, nullable=False, index=True)
