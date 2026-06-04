"""OAuth request/response DTOs (ORAA-4 §21 schema layer)."""

from __future__ import annotations

from pydantic import BaseModel


class LoginUrlResponse(BaseModel):
    authorize_url: str
