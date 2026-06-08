"""Integration-key ORM model (ORAA-4 §21 models layer) — the R6 stateful authz floor (ADR-019).

One row per issued integration key. The key itself is never stored: only its non-secret lookup
``key_prefix`` (UNIQUE), its SHA-256 ``key_hash`` (constant-time compared), and ``last4`` (display).
Tenancy is ``organisation_id`` (NOT NULL — a key always carries a real org; the legacy empty-org
placeholder is deliberately not inherited). Exactly one binding is set — a published-agent slug XOR
capability allow-list — which is the per-key authorization the gateway enforces before forwarding.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from oraclous_application_gateway_service.models.base_model import BaseModel


class IntegrationKey(BaseModel):
    __tablename__ = "integration_keys"
    __table_args__ = (
        CheckConstraint(
            "(bound_agent_slug IS NOT NULL) <> (capability_allow_list IS NOT NULL)",
            name="ck_integration_keys_exactly_one_binding",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    key_prefix = Column(String(32), nullable=False, unique=True, index=True)
    key_hash = Column(Text, nullable=False)
    last4 = Column(String(4))
    # exactly one binding (CHECK above) — the per-key authz the gateway enforces pre-forward.
    # none_as_null so Python None binds SQL NULL (not the JSONB value 'null', which would satisfy
    # `IS NOT NULL` and break the exactly-one-binding CHECK).
    bound_agent_slug = Column(String, nullable=True)
    capability_allow_list = Column(JSONB(none_as_null=True), nullable=True)
    # per-key edge policy (enforcement lands in later slices: CORS in S5)
    cors_origins = Column(JSONB(none_as_null=True), nullable=True)
    rate_limit = Column(Integer, nullable=True)
    rate_window_seconds = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="active")  # 'active' | 'revoked'
    expires_at = Column(DateTime(timezone=True), nullable=True)  # optional TTL; None = no expiry
