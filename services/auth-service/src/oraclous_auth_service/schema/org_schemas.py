"""Organisation request/response DTOs (ORAA-4 §21 schema layer).

No ``organisation_id`` is accepted off a request body (it is the path/route, never input); these are
plain create/update payloads + the org projection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class UpdateOrgRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    logo_url: str | None = Field(default=None, max_length=512)


class OrgResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None = None
    logo_url: str | None = None
    owner_user_id: str
    status: str


# Role changes are constrained to non-owner roles — ownership transfer is a separate concern.
class UpdateMemberRoleRequest(BaseModel):
    role: Literal["admin", "member"]


class OrgMemberResponse(BaseModel):
    user_id: str
    email: str | None = None
    role: str
    since: datetime
