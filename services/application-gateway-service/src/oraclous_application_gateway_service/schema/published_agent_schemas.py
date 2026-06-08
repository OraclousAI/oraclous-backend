"""Published-agent management shapes (ORAA-4 §21 schema layer)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PublishAgentRequest(BaseModel):
    slug: str
    bound_capability_ref: str  # the capability/harness descriptor the harness runs on invoke
    display_name: str | None = None
    description: str | None = None


class PublishedAgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    bound_capability_ref: str
    display_name: str | None = None
    description: str | None = None
    status: str
    created_at: datetime | None = None
