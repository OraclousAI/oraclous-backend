"""Integration-key management shapes (ORAA-4 §21 schema layer) — the member-managed CRUD surface."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oraclous_application_gateway_service.schema.published_agent_schemas import SLUG_PATTERN

# a CORS Origin is scheme://host[:port], NO trailing slash/path — it is matched EXACTLY against the
# browser Origin header, so a stored "https://x.com/" would silently never match. Reject it early.
_ORIGIN_RE = re.compile(
    r"^https?://[a-zA-Z0-9.-]+(:\d+)?\Z"
)  # \Z not $ — reject a trailing newline


class MintKeyRequest(BaseModel):
    # exactly one binding (store CHECK): a published-agent slug XOR a capability allow-list
    bound_agent_slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    capability_allow_list: list[str] | None = None
    cors_origins: list[str] | None = None
    rate_limit: int | None = None
    rate_window_seconds: int | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> MintKeyRequest:
        if (self.bound_agent_slug is None) == (self.capability_allow_list is None):
            raise ValueError("supply exactly one of 'bound_agent_slug' or 'capability_allow_list'")
        for origin in self.cors_origins or []:
            if not _ORIGIN_RE.match(origin):
                raise ValueError(
                    f"invalid CORS origin {origin!r}: want scheme://host[:port], no trailing slash"
                )
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
