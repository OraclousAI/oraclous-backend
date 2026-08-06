"""Credential-broker seam (services layer).

KGS resolves a SQL ingest's ``connection_string`` credential by ``credential_id`` from the
credential-broker — it never decrypts or stores secrets (ADR-008 operator separation). Mirrors the
capability-registry's ``PostgreSQLReader`` credential path (which already resolves a stored
``connection_string`` by id via the broker's ``/internal/resolve-credential`` with the internal key)
and the CRS ``credential_client``'s two implementations:

* ``RealCredentialBroker`` — POSTs ``/internal/resolve-credential`` with ``X-Internal-Key``; returns
  the decrypted ``{"connection_string": "..."}`` payload.
* ``FakeCredentialBroker`` — deterministic, key-free; selected by ``KGS_CREDENTIAL_BROKER_MODE``
  ``=fake`` (the dev/CI default) so the SQL-ingest path reaches a real end-to-end test broker-free.

``credential_id`` is supplied at ingest-request time (never stored with a connector); the org is
server-injected (the caller cannot override it).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import httpx


class CredentialResolutionError(Exception):
    """The broker could not resolve the connection_string credential (a configuration failure)."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class CredentialBrokerPort(Protocol):
    async def resolve_connection_string(
        self, *, organisation_id: str, credential_id: str
    ) -> str: ...

    async def resolve_credential(
        self, *, credential_id: str, organisation_id: uuid.UUID
    ) -> dict[str, Any]:
        """The decrypted payload by id, org-scoped (#724). Satisfies ``ModelCredentialBrokerPort``
        so ``resolve_model_credential`` can take this client directly."""
        ...

    async def org_default_credential_id(
        self, *, organisation_id: uuid.UUID, purpose: str = "model"
    ) -> str | None:
        """The org's designated default credential id for ``purpose``, or None (#724)."""
        ...

    async def aclose(self) -> None: ...


class FakeCredentialBroker:
    """Deterministic, key-free broker for dev/CI: returns a configured DSN regardless of id."""

    def __init__(
        self, *, dsn_by_id: dict[str, str] | None = None, default_dsn: str | None = None
    ) -> None:
        self._dsn_by_id = dsn_by_id or {}
        self._default_dsn = default_dsn
        self.closed = False

    async def resolve_connection_string(
        self,
        *,
        organisation_id: str,
        credential_id: str,  # noqa: ARG002 — fake ignores org
    ) -> str:
        dsn = self._dsn_by_id.get(credential_id, self._default_dsn)
        if not dsn:
            raise CredentialResolutionError(
                f"fake broker has no DSN mapped for credential {credential_id!r}",
                error_code="credential_not_found",
            )
        return dsn

    async def resolve_credential(
        self,
        *,
        credential_id: str,
        organisation_id: uuid.UUID,  # noqa: ARG002 — fake ignores org
    ) -> dict[str, Any]:
        """#724 dev/CI seam. Returns a deterministic api_key so a key-free stack still exercises
        the resolution PATH; it never reaches a real provider."""
        return {"api_key": f"fake-model-key-for-{credential_id}"}

    async def org_default_credential_id(
        self,
        *,
        organisation_id: uuid.UUID,  # noqa: ARG002 — fake ignores org
        purpose: str = "model",
    ) -> str | None:
        return f"fake-default-{purpose}"

    async def aclose(self) -> None:
        self.closed = True  # no client to close; mark for symmetry with the real broker


class RealCredentialBroker:
    """Resolves a stored connection_string by id against the running credential-broker."""

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

    async def resolve_connection_string(self, *, organisation_id: str, credential_id: str) -> str:
        if not credential_id:
            raise CredentialResolutionError(
                "no credential_id supplied for the SQL ingest", error_code="credential_not_mapped"
            )
        resp = await self._client.post(
            "/internal/resolve-credential",
            json={"organisation_id": organisation_id, "credential_id": credential_id},
        )
        if resp.status_code == 404:
            raise CredentialResolutionError(
                "credential not found in the broker", error_code="credential_not_found"
            )
        if resp.status_code != 200:
            raise CredentialResolutionError(
                f"broker resolve-credential returned {resp.status_code}", error_code="broker_error"
            )
        payload = resp.json().get("credential", {})
        dsn = payload.get("connection_string")
        if not dsn:
            raise CredentialResolutionError(
                "resolved credential has no connection_string", error_code="credential_wrong_type"
            )
        return dsn

    async def resolve_credential(
        self, *, credential_id: str, organisation_id: uuid.UUID
    ) -> dict[str, Any]:
        """The decrypted payload by id (#724), org-scoped. Raises on every non-200 so the caller's
        fail-closed path fires rather than a partial payload being used."""
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
        payload: dict[str, Any] = resp.json().get("credential", {})
        return payload

    async def org_default_credential_id(
        self, *, organisation_id: uuid.UUID, purpose: str = "model"
    ) -> str | None:
        """The org's designated default credential id (#724), or None when nothing is designated.

        A null answer is NOT an error: it is the fail-closed signal the caller turns into a typed
        refusal naming what the user must configure.
        """
        resp = await self._client.post(
            "/internal/org-default-credential",
            json={"organisation_id": str(organisation_id), "purpose": purpose},
        )
        if resp.status_code != 200:
            raise CredentialResolutionError(
                f"broker org-default-credential returned {resp.status_code}",
                error_code="broker_error",
            )
        value = resp.json().get("credential_id")
        return str(value) if value else None

    async def aclose(self) -> None:
        await self._client.aclose()


def make_credential_broker(settings) -> CredentialBrokerPort:
    """Build the broker from config: real (the default) or fake (explicit dev/CI opt-in, #653)."""
    if settings.credential_broker_mode == "fake":
        return FakeCredentialBroker(default_dsn=settings.credential_broker_fake_dsn)
    if not settings.credential_broker_base_url or not settings.internal_service_key:
        raise CredentialResolutionError(
            "real broker requires KGS_CREDENTIAL_BROKER_BASE_URL + KGS_INTERNAL_SERVICE_KEY",
            error_code="broker_misconfigured",
        )
    return RealCredentialBroker(
        base_url=settings.credential_broker_base_url,
        internal_key=settings.internal_service_key,
    )
