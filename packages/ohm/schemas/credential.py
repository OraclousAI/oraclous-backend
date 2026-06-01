import enum

from pydantic import BaseModel, ConfigDict, model_validator


class CredentialType(enum.StrEnum):
    OAUTH_TOKEN = "oauth_token"  # noqa: S105
    API_KEY = "api_key"
    CONNECTION_STRING = "connection_string"
    USERNAME_PASSWORD = "username_password"  # noqa: S105


class CredentialRequirement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: CredentialType
    provider: str
    scopes: list[str] | None = None

    @model_validator(mode="after")
    def _validate_oauth_scopes(self) -> "CredentialRequirement":
        # T2-M3: oauth_token must explicitly declare at least one scope
        if self.type == CredentialType.OAUTH_TOKEN:
            if not self.scopes:
                raise ValueError(
                    "oauth_token credential_requirements must declare at least one scope (T2-M3)"
                )
        return self
