"""Published-agent public surface shapes (ORAA-4 §21 schema layer) — the integration-key plane.

Deliberately NARROW projections: the public metadata + invoke response expose only display + result
fields, never the gateway/harness internals (org id, harness/content ids, step transcript, token
counts) that the upstream ``HarnessExecutionOut`` carries.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class PublicAgentOut(BaseModel):
    """What an integration-key holder may see about its bound published agent (no internal ids)."""

    slug: str
    display_name: str | None = None
    description: str | None = None


class InvokeRequest(BaseModel):
    input: str = Field(min_length=1)  # the goal/message handed to the agent (harness ``input``)


class InvokeResponse(BaseModel):
    """A narrow projection of the harness execution — result only, no internals."""

    execution_id: uuid.UUID
    status: str
    output: str | None = None
    error: str | None = None
