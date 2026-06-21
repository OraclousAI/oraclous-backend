"""Published-agent management shapes (schema layer)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# a URL-safe slug: lowercase alphanumeric + hyphens, starting alphanumeric. Shared with the key's
# bound_agent_slug so a key can only ever be bound to a publishable slug.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class PublishAgentRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    bound_capability_ref: str = Field(min_length=1)  # the descriptor the harness runs on invoke
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
