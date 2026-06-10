"""``OrgDataKey`` storage model (ADR-020 — per-org envelope encryption).

One row per organisation: the org's data-encryption key (DEK), stored ONLY in its KEK-wrapped form
(``wrapped_dek``, base64). The plaintext DEK is never persisted — it is unwrapped transiently via
the KMS and cached in-process. ``kek_provider``/``kek_key_id`` record which KEK wrapped it (so a
wrap can be traced + re-wrapped on a KEK rotation). ``organisation_id`` is UNIQUE (the per-tenant
boundary).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from oraclous_credential_broker_service.models.base_model import BaseModel


class OrgDataKey(BaseModel):
    __tablename__ = "org_data_keys"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(PG_UUID(as_uuid=True), nullable=False, unique=True, index=True)
    wrapped_dek = Column(String, nullable=False)  # base64(KEK-wrapped DEK); plaintext never stored
    kek_provider = Column(String, nullable=False)  # "local" | "aws"
    kek_key_id = Column(String, nullable=False)  # the KEK identifier that produced this wrap
