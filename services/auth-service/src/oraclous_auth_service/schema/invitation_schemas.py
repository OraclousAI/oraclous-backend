"""Invitation request/response DTOs (ORAA-4 §21 schema layer).

The raw token is returned exactly once (in ``CreateInvitationResponse.token``); it is never echoed
again. Peek/accept take the token in the body (never the URL) so it is not logged (T-INVITE/leak).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateInvitationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default="member")
    subgraph_grants: dict | None = None


class CreateInvitationResponse(BaseModel):
    id: str
    organisation_id: str
    email: str
    role: str
    status: str
    token: str  # raw — returned exactly once


class InvitationResponse(BaseModel):
    id: str
    organisation_id: str
    email: str
    role: str
    status: str


class PeekInvitationRequest(BaseModel):
    token: str = Field(min_length=1)


class InvitationPeekResponse(BaseModel):
    organisation_id: str
    organisation_name: str | None = None
    email: str
    role: str
    status: str


class AcceptInvitationRequest(BaseModel):
    token: str = Field(min_length=1)


class AcceptInvitationResponse(BaseModel):
    organisation_id: str
    role: str
