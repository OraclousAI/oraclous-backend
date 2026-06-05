"""Credential-broker seam (ORAA-4 §21 services layer; reshape of legacy
``oraclous-core-service/app/services/credential_client.py``).

The capability registry is a service-to-service caller of the credential-broker — it never decrypts
or stores secrets (ADR-008 operator separation). ``CredentialBrokerPort`` resolves a tool's declared
credential requirement into the payload the executor needs. Two implementations:

* ``RealCredentialBroker`` — talks to the broker over ``/internal/*`` with ``X-Internal-Key``.
  OAuth provider tokens resolve via ``/internal/runtime-token``; broadening non-OAuth resolution to
  the real broker lands with the S5 real-broker integration.
* ``FakeCredentialBroker`` — deterministic, key-free; selected by ``CREDENTIAL_BROKER_MODE=fake``
  (the dev/CI default) so slices 4–5 reach a real end-to-end smoke without external provider keys.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import httpx
from pydantic import BaseModel


class ResolvedCredential(BaseModel):
    credential_type: str
    payload: dict[str, Any]  # the material the executor consumes (e.g. {"connection_string": ...})


class CredentialResolutionError(Exception):
    """The broker could not resolve a required credential (surfaced as a configuration failure)."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        login_url: str | None = None,
        missing_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.login_url = login_url
        self.missing_scopes = missing_scopes or []


class CredentialBrokerPort(Protocol):
    async def resolve(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        requirement: dict[str, Any],
        credential_id: str | None = None,
    ) -> ResolvedCredential: ...

    async def aclose(self) -> None: ...


def _libpq_dsn(database_url: str) -> str:
    """Coerce a SQLAlchemy async DSN to a plain libpq DSN asyncpg can connect with."""
    return database_url.replace("+asyncpg", "").replace("+psycopg", "")


class FakeCredentialBroker:
    """Deterministic, key-free broker for dev/CI: real execution without external provider keys."""

    def __init__(self, *, fake_db_dsn: str) -> None:
        self._fake_db_dsn = fake_db_dsn
        self._closed = False

    async def resolve(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        requirement: dict[str, Any],
        credential_id: str | None = None,  # noqa: ARG002 — fake ignores the stored id
    ) -> ResolvedCredential:
        ctype = requirement.get("type")
        provider = requirement.get("provider", "")
        if ctype == "connection_string":
            return ResolvedCredential(
                credential_type=ctype, payload={"connection_string": self._fake_db_dsn}
            )
        if ctype == "oauth_token":
            return ResolvedCredential(
                credential_type=ctype,
                payload={
                    "access_token": f"fake-{provider}-access-token",
                    "scopes": list(requirement.get("scopes", [])),
                },
            )
        if ctype == "api_key":
            return ResolvedCredential(
                credential_type=ctype, payload={"api_key": f"fake-{provider}-api-key"}
            )
        if ctype == "username_password":
            return ResolvedCredential(
                credential_type=ctype, payload={"username": "fake-user", "password": "fake-pass"}
            )
        raise CredentialResolutionError(
            f"unknown credential type '{ctype}'", error_code="unknown_credential_type"
        )

    async def aclose(self) -> None:
        self._closed = True  # no network client to close; mark for symmetry with the real broker


class RealCredentialBroker:
    """Resolves credentials against the running credential-broker over ``/internal/*``."""

    def __init__(
        self,
        *,
        base_url: str,
        internal_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Internal-Key": internal_key, "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def resolve(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        requirement: dict[str, Any],
        credential_id: str | None = None,
    ) -> ResolvedCredential:
        ctype = requirement.get("type")
        if ctype == "oauth_token":
            resp = await self._client.post(
                "/internal/runtime-token",
                json={
                    "organisation_id": str(organisation_id),
                    "user_id": str(user_id),
                    "provider": requirement.get("provider", ""),
                    "required_scopes": list(requirement.get("scopes", [])),
                },
            )
            if resp.status_code != 200:
                raise CredentialResolutionError(
                    f"broker runtime-token returned {resp.status_code}",
                    error_code="broker_error",
                )
            body = resp.json()
            if not body.get("success"):
                raise CredentialResolutionError(
                    body.get("error_code") or "oauth_resolution_failed",
                    error_code=body.get("error_code") or "oauth_resolution_failed",
                    login_url=body.get("login_url"),
                    missing_scopes=body.get("missing_scopes"),
                )
            return ResolvedCredential(
                credential_type=ctype,
                payload={
                    "access_token": body.get("access_token"),
                    "scopes": body.get("scopes", []),
                },
            )
        # Non-OAuth (connection_string / api_key / username_password): resolve the stored
        # credential's decrypted payload by id via the broker's internal endpoint (X-Internal-Key).
        if not credential_id:
            raise CredentialResolutionError(
                f"no credential is mapped for '{ctype}'", error_code="credential_not_mapped"
            )
        resp = await self._client.post(
            "/internal/resolve-credential",
            json={"organisation_id": str(organisation_id), "credential_id": credential_id},
        )
        if resp.status_code == 404:
            raise CredentialResolutionError(
                "credential not found in the broker", error_code="credential_not_found"
            )
        if resp.status_code != 200:
            raise CredentialResolutionError(
                f"broker resolve-credential returned {resp.status_code}", error_code="broker_error"
            )
        return ResolvedCredential(credential_type=ctype, payload=resp.json()["credential"])

    async def aclose(self) -> None:
        await self._client.aclose()
