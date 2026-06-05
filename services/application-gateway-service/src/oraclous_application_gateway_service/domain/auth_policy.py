"""Edge auth policy (ORAA-4 §21 domain layer) — pure, no I/O.

The closed allow-list of UNAUTHENTICATED public prefixes. Token issuance is unauthenticated by
definition (login/register/refresh, the OAuth dance), so these proxy through without an edge JWT;
every other routed path requires a verified principal.
"""

from __future__ import annotations

_PUBLIC_PREFIXES: tuple[str, ...] = ("/v1/auth", "/oauth")


def is_public(path: str) -> bool:
    """True if the path is on the public allow-list (no edge auth required)."""
    return any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES)
