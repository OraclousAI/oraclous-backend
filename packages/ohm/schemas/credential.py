from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, model_validator


class CredentialType(str, Enum):
    OAUTH_TOKEN = "oauth_token"
    API_KEY = "api_key"
    CONNECTION_STRING = "connection_string"
    USERNAME_PASSWORD = "username_password"


class CredentialRequirement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: CredentialType
    provider: str
    scopes: Optional[list[str]] = None
    # required is a legacy field from the core-service ToolDefinition schema; preserved for round-trip migration (ORAA-106)
    required: bool = True

    @model_validator(mode="after")
    def _validate_oauth_scopes(self) -> "CredentialRequirement":
        # T2-M3: oauth_token must explicitly declare at least one non-empty scope
        if self.type == CredentialType.OAUTH_TOKEN:
            if not self.scopes or any(s.strip() == "" for s in self.scopes):
                raise ValueError(
                    "oauth_token credential_requirements must declare at least one non-empty scope (T2-M3)"
                )
        return self
