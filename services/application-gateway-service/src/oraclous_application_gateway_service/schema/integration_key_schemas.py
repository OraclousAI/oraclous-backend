"""Integration-key management shapes (ORAA-4 §21 schema layer) — the member-managed CRUD surface."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator


class MintKeyRequest(BaseModel):
    # exactly one binding (store CHECK): a published-agent slug XOR a capability allow-list
    bound_agent_slug: str | None = None
    capability_allow_list: list[str] | None = None
    cors_origins: list[str] | None = None
    rate_limit: int | None = None
    rate_window_seconds: int | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one_binding(self) -> MintKeyRequest:
        if (self.bound_agent_slug is None) == (self.capability_allow_list is None):
            raise ValueError("supply exactly one of 'bound_agent_slug' or 'capability_allow_list'")
        return self


class MintedKeyResponse(BaseModel):
    """Returned ONCE on mint/rotate — carries the plaintext secret, never stored or shown again."""

    id: uuid.UUID
    key: str  # the plaintext token (oak-…); shown once
    key_prefix: str
    last4: str | None = None
    bound_agent_slug: str | None = None
    capability_allow_list: list[str] | None = None
    status: str


class KeyOut(BaseModel):
    """Redacted view for list/get — never the hash, never the plaintext."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key_prefix: str
    last4: str | None = None
    bound_agent_slug: str | None = None
    capability_allow_list: list[str] | None = None
    cors_origins: list[str] | None = None
    rate_limit: int | None = None
    status: str
    expires_at: datetime | None = None
    created_at: datetime | None = None
