"""Auth-service membership seam (services layer) — #464 / Interface-Contract (ADR-017 + ADR-036-G2).

Before writing a cross-org grant edge, KGS must verify the grantee user actually belongs to the
grantee organisation (else the stored grantee scope / audit trail carries a spoofable value). The
check-data lives only in auth-service (``OrgMemberRepository``); this client calls its internal-key-
gated ``GET /internal/v1/orgs/{org}/members/{user}`` → ``{"is_member": bool}``.

Mirrors the ``credential_client`` two-impl pattern:

* ``RealAuthClient`` — GETs the internal endpoint with ``X-Internal-Key``; returns ``is_member``.
  The DEPLOYED stack runs this (``KGS_AUTH_CLIENT_MODE=real`` + ``KGS_AUTH_SERVICE_URL``), so the
  grant check is genuinely validated against the real ``OrgMemberRepository`` (NOT dev-org-for-all).
* ``FakeAuthClient`` — deterministic, key-free (the ``fake`` mode, for CI with no auth service).
  ``members=None`` (the ``make_auth_client`` default) allows all — back-compat so a CI grant test is
  unaffected; an explicit ``members`` set is the unit-test seam that exercises the reject path.

FAIL-CLOSED (Reza, 2026-07-03): ``verify_membership`` raises ``AuthServiceUnavailable`` on any
transport/HTTP error — a cross-org write is never granted un-validated (→ 503). Never fail-open.
"""

from __future__ import annotations

from typing import Protocol

import httpx


class AuthServiceUnavailable(Exception):
    """The auth-service membership check could not be completed — fail-closed (→ 503)."""


class AuthMembershipPort(Protocol):
    async def verify_membership(self, *, organisation_id: str, user_id: str) -> bool: ...

    async def aclose(self) -> None: ...


class FakeAuthClient:
    """Deterministic, key-free membership checker for CI. ``members=None`` allows all (back-compat
    default); an explicit set of ``(organisation_id, user_id)`` pairs treats only those as members
    (the unit-test seam that drives the reject path)."""

    def __init__(self, *, members: set[tuple[str, str]] | None = None) -> None:
        self._members = members
        self.closed = False

    async def verify_membership(self, *, organisation_id: str, user_id: str) -> bool:
        if self._members is None:  # unconfigured fake → allow all (a CI grant test is unaffected)
            return True
        return (organisation_id, user_id) in self._members

    async def aclose(self) -> None:
        self.closed = True


class RealAuthClient:
    """Resolves grantee membership against the running auth-service internal endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        internal_key: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Internal-Key": internal_key},
            timeout=timeout,
            transport=transport,
        )

    async def verify_membership(self, *, organisation_id: str, user_id: str) -> bool:
        try:
            resp = await self._client.get(
                f"/internal/v1/orgs/{organisation_id}/members/{user_id}"
            )  # fail-closed below on any non-200
        except httpx.HTTPError as exc:  # transport/timeout — fail-closed, never grant un-validated
            raise AuthServiceUnavailable("auth-service membership check unreachable") from exc
        if resp.status_code != 200:  # any non-200 (incl. a 401 mis-key) is fail-closed
            raise AuthServiceUnavailable(
                f"auth-service membership check returned {resp.status_code}"
            )
        return bool(resp.json().get("is_member"))

    async def aclose(self) -> None:
        await self._client.aclose()


def make_auth_client(settings: object) -> AuthMembershipPort:
    """Build the membership client from config: fake (dev/CI default) or real (talks to auth)."""
    mode = getattr(settings, "auth_client_mode", "fake")
    if mode == "fake":
        return FakeAuthClient()
    base_url = getattr(settings, "auth_service_url", None)
    internal_key = getattr(settings, "internal_service_key", None)
    if not base_url or not internal_key:
        raise AuthServiceUnavailable(
            "real auth client requires KGS_AUTH_SERVICE_URL + KGS_INTERNAL_SERVICE_KEY"
        )
    return RealAuthClient(base_url=base_url, internal_key=internal_key)
