"""Credential schemas (reshape of legacy ``app/schema/credential_schema.py``).

``organisation_id`` is deliberately **not** a field on any of these inbound
schemas: it is resolved from the authenticated principal context and passed
explicitly to the repository, never trusted from a request body (ORG001
guardrail / ORA-40 security-architect ruling, ADR-006).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class RequestCredentials(BaseModel):
    user_id: UUID
    tool_id: UUID | None = None


class CreateCredential(BaseModel):
    tool_id: UUID
    user_id: UUID
    name: str | None = None
    provider: str
    cred_type: Literal["oauth", "api_key", "raw"]
    credential: dict


class CredentialsUpdate(BaseModel):
    id: UUID
    name: str | None = None
    provider: str
    user_id: UUID
    tool_id: UUID
    cred_type: Literal["oauth", "api_key", "raw"]
    credential: dict


class CredentialOut(BaseModel):
    """Create/update response — metadata only; the secret is never echoed back here."""

    id: UUID
    name: str | None = None
    provider: str
    user_id: UUID
    tool_id: UUID
    cred_type: str


class RequestCredentialsResponse(BaseModel):
    """Read response — includes the DECRYPTED credential (only on explicit retrieval)."""

    id: UUID
    name: str | None = None
    provider: str
    user_id: UUID
    tool_id: UUID
    cred_type: str
    credential: dict
