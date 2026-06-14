"""Credential schemas (reshape of legacy ``app/schema/credential_schema.py``).

``organisation_id`` is deliberately **not** a field on any of these inbound
schemas: it is resolved from the authenticated principal context and passed
explicitly to the repository, never trusted from a request body (ORG001
guardrail / ORA-40 security-architect ruling, ADR-006).
"""

from __future__ import annotations

from datetime import datetime
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
    # Optional: omit to preserve the stored secret (name-only rename); when set, rotates it.
    credential: dict | None = None


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


class RuntimeTokenInput(BaseModel):
    """Internal (X-Internal-Key) runtime-token request. ``*Input`` (not ``*Request``): the trusted
    caller supplies ``organisation_id`` — this is service→service plumbing, not a public body."""

    organisation_id: UUID
    user_id: UUID
    provider: str
    required_scopes: list[str] | None = None


class EnsureDataSourceInput(BaseModel):
    """Internal runtime-token request scoped to a data source (scopes from the catalogue)."""

    organisation_id: UUID
    user_id: UUID
    provider: str
    data_source: str


class RuntimeTokenResponse(BaseModel):
    """Success/error union for runtime-token resolution."""

    success: bool
    access_token: str | None = None
    expires_at: str | None = None
    scopes: list[str] = []
    provider: str = ""
    error_code: str | None = None
    missing_scopes: list[str] | None = None
    login_url: str | None = None


class MintDelegatedTokenInput(BaseModel):
    """Internal (X-Internal-Key) delegated-token mint. Trusted caller supplies organisation_id."""

    organisation_id: UUID
    member_id: UUID
    agent_id: UUID
    scopes: list[str]
    expires_at: datetime


class DelegatedTokenMintResponse(BaseModel):
    token: str  # the raw bearer — returned exactly once
    token_id: UUID
    member_id: UUID
    agent_id: UUID
    scopes: list[str]
    expires_at: datetime


class ValidateDelegatedTokenInput(BaseModel):
    """Internal per-use validation of a delegated token."""

    organisation_id: UUID
    raw_token: str
    requesting_agent_id: UUID
    requested_scopes: list[str]


class DelegationValidationResponse(BaseModel):
    success: bool
    reason: str | None = None
    token_id: UUID | None = None
    member_id: UUID | None = None
    agent_id: UUID | None = None
    granted_scopes: list[str] | None = None


class RevokeDelegatedTokenInput(BaseModel):
    organisation_id: UUID


class ResolveCredentialInput(BaseModel):
    """Internal (X-Internal-Key) resolution of a stored credential's secret by id. ``*Input`` (not
    ``*Request``): the trusted caller supplies ``organisation_id`` — service→service plumbing."""

    organisation_id: UUID
    credential_id: UUID


class ResolveCredentialResponse(BaseModel):
    """The decrypted credential payload for a trusted service caller (e.g. a connection_string)."""

    credential_id: UUID
    provider: str
    cred_type: str
    credential: dict
