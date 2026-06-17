"""``OrgDataKey`` storage model (ADR-020 — per-org envelope encryption).

One row per organisation: the org's data-encryption key (DEK), stored ONLY in its KEK-wrapped form
(``wrapped_dek``, base64). The plaintext DEK is never persisted — it is unwrapped transiently via
the KMS and cached in-process. ``kek_provider``/``kek_key_id`` record which KEK wrapped it (so a
wrap can be traced + re-wrapped on a KEK rotation). ``organisation_id`` is UNIQUE (the per-tenant
boundary).

No ``from __future__ import annotations`` — SQLAlchemy resolves the ``Mapped[...]`` annotations at
mapper configuration, so they must be real types.
"""

import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from oraclous_credential_broker_service.models.base_model import BaseModel


class OrgDataKey(BaseModel):
    __tablename__ = "org_data_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    wrapped_dek: Mapped[str] = mapped_column(
        String, nullable=False
    )  # base64(KEK-wrapped DEK); plaintext never stored
    kek_provider: Mapped[str] = mapped_column(String, nullable=False)  # "local" | "aws"
    kek_key_id: Mapped[str] = mapped_column(
        String, nullable=False
    )  # the KEK identifier that produced this wrap
