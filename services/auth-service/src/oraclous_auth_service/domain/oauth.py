"""OAuth domain helpers (domain layer, threat T-OAUTH). Pure, no I/O.

PKCE (S256) generation, opaque random state, and scope set-union merge. The verifier is kept
server-side (an ``oauth_states`` row); only the challenge is sent to the provider.
"""

from __future__ import annotations

import base64
import hashlib
import secrets


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    """An opaque, single-use, unguessable state value (round-trips via the provider)."""
    return _b64url(secrets.token_bytes(32))


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` using the S256 method (RFC 7636)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def is_allowed_redirect_uri(
    redirect_uri: str, allow_list: tuple[str, ...], *, permissive_when_empty: bool
) -> bool:
    """Whether a client-supplied ``redirect_uri`` is permitted (WP-11, T-OAUTH open-redirect).

    A non-empty allow-list is enforced by EXACT string match (no prefix/host games — an open
    redirect lives in the difference between "starts with" and "equals"). An EMPTY allow-list is
    governed by ``permissive_when_empty``: ``True`` in dev (allow any redirect so the local stack /
    CI keep working without configuring callbacks), ``False`` in prod (deny every redirect — fail
    closed, forcing the deploy to declare its real callbacks)."""
    if not allow_list:
        return permissive_when_empty
    return redirect_uri in allow_list


def merge_scopes(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Set-union of granted scopes, order-stable (existing first, then newly-added)."""
    seen: set[str] = set()
    out: list[str] = []
    for scope in (existing or []) + (new or []):
        if scope and scope not in seen:
            seen.add(scope)
            out.append(scope)
    return out
