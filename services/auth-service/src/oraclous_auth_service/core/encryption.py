"""Symmetric encryption for OAuth tokens at rest (ORAA-4 §21 core layer, threat T-OAUTH-PLAINTEXT).

AES-256-GCM (authenticated). The 32-byte key comes from ``OAUTH_ENC_KEY`` (urlsafe-base64); a fixed
dev default keeps the local stack key-free. Each ``encrypt`` uses a fresh 96-bit nonce; the stored
value is ``base64(nonce || ciphertext||tag)``. Provider access/refresh tokens are never persisted in
the clear.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from oraclous_governance import require_secret

# Dev-only default key (decodes to exactly 32 bytes). Production injects OAUTH_ENC_KEY via a secret.
_DEV_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # noqa: S105 — dev default, not a secret
_NONCE_LEN = 12


def _key() -> bytes:
    # Fail closed in prod (RUN_MODE=prod): a missing/empty OAUTH_ENC_KEY raises rather than silently
    # falling back to the in-source dev key. The empty-string-is-missing rule closes the old
    # `os.environ.get(...) or _DEV_KEY` bug (an empty prod env var no longer reaches the dev key).
    raw = require_secret("OAUTH_ENC_KEY", dev_default=_DEV_KEY)
    key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if len(key) != 32:
        raise ValueError("OAUTH_ENC_KEY must decode to 32 bytes (AES-256)")
    return key


def encrypt(plaintext: str) -> str:
    """Return ``base64(nonce || AES-256-GCM(plaintext))``. Empty input → empty output."""
    if not plaintext:
        return ""
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(token: str) -> str:
    """Inverse of :func:`encrypt`. Empty input → empty output."""
    if not token:
        return ""
    blob = base64.urlsafe_b64decode(token)
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(_key()).decrypt(nonce, ct, None).decode("utf-8")
