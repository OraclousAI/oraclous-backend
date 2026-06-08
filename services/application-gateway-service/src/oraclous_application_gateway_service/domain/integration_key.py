"""Integration-key crypto-shape helpers (ORAA-4 §21 domain layer) — pure, no I/O.

Token format: ``<scheme>-<prefix>-<secret>`` where ``scheme`` ∈ {``oak`` (org key), ``oag`` (agent
key)}, ``prefix`` is a 16-hex **non-secret** lookup id (UNIQUE per key, used to find the row), and
``secret`` is high-entropy. The FULL token is SHA-256'd for storage; lookup is by prefix, verify
by constant-time hash compare. The plaintext is shown to the owner exactly once and never stored.

(SHA-256 + constant-time compare is the proven legacy shape; an HMAC-with-pepper is the hardened-DoD
tightening, tracked as a follow-up — not shipped here.)
"""

from __future__ import annotations

import hashlib
import secrets
from typing import NamedTuple

_SCHEMES = ("oak", "oag")
_PREFIX_HEX_BYTES = 8  # -> 16 hex chars
PREFIX_LEN = _PREFIX_HEX_BYTES * 2


class MintedKey(NamedTuple):
    plaintext: str  # returned to the owner ONCE; never stored
    key_prefix: str
    key_hash: str
    last4: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_key(scheme: str = "oak") -> MintedKey:
    if scheme not in _SCHEMES:
        raise ValueError(f"scheme must be one of {_SCHEMES}")
    prefix = secrets.token_hex(_PREFIX_HEX_BYTES)
    secret = secrets.token_urlsafe(32)
    plaintext = f"{scheme}-{prefix}-{secret}"
    return MintedKey(plaintext, prefix, hash_token(plaintext), secret[-4:])


def is_integration_key(token: str) -> bool:
    return any(token.startswith(scheme + "-") for scheme in _SCHEMES)


def prefix_of(token: str) -> str | None:
    """The fixed-width non-secret lookup prefix embedded in a token, or None if malformed."""
    if not is_integration_key(token):
        return None
    rest = token[4:]  # after the ``oak-`` / ``oag-`` scheme
    prefix = rest[:PREFIX_LEN]
    return prefix if len(prefix) == PREFIX_LEN else None


def verify_key(token: str, key_hash: str) -> bool:
    """Constant-time compare of the presented token against the stored hash (no timing oracle)."""
    return secrets.compare_digest(hash_token(token), key_hash)
