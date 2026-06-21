"""Auth request/response DTOs (schema layer).

Email is a constrained ``str`` (a lightweight ``@`` shape check) rather than ``EmailStr`` to keep
the slice free of the ``email-validator`` dependency; the repository normalises to lowercase. None
of these request models carry ``organisation_id`` (it is server-resolved from the credential/token,
never accepted off the body — the org-scoping guardrail's contract).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class _EmailMixin(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or v.startswith("@") or v.endswith("@") or " " in v:
            raise ValueError("invalid email address")
        return v


class RegisterRequest(_EmailMixin):
    password: str = Field(min_length=8, max_length=72)
    # Optional human name; its first token names the default org "{First}'s Second Mind" (#317).
    # Absent/blank falls back to the email local-part, so the org name is never blank.
    full_name: str | None = Field(default=None, max_length=320)


class LoginRequest(_EmailMixin):
    password: str = Field(min_length=1, max_length=72)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=72)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — OAuth2 token-type scheme name, not a secret
    expires_in: int
    email: str
    is_superuser: bool


class MeResponse(BaseModel):
    id: str
    principal_type: str
    organisation_id: str
    email: str | None = None
    org_role: str | None = None  # the member's role in organisation_id (R7-SEC S2); None pre-S2
