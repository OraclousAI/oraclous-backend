"""AES-256-GCM credential encryption (core layer, threat T6 / plaintext-at-rest).

Lifted from the legacy ``credential-broker-service/app/core/security.py``: the base64
``ENCRYPTION_KEY`` decodes to 32 bytes (AES-256); a fresh 96-bit nonce per encrypt; the stored value
is ``hex(nonce || ciphertext||tag)``. Reversible (unlike bcrypt) — correct for the OAuth/API secrets
the broker hands back at runtime. Fails closed if the key is absent or the wrong length.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from oraclous_credential_broker_service.core.config import get_settings

_NONCE_LEN = 12


def _key() -> bytes:
    key = base64.b64decode(get_settings().ENCRYPTION_KEY)
    if len(key) != 32:
        raise ValueError("ENCRYPTION_KEY must decode to 32 bytes (AES-256)")
    return key


def encrypt_secret(secret: Any) -> str:
    """Encrypt a JSON-serialisable secret → ``hex(nonce||ciphertext)``."""
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_key()).encrypt(nonce, json.dumps(secret).encode("utf-8"), None)
    return (nonce + ct).hex()


def decrypt_secret(encrypted_hex: str) -> Any:
    """Inverse of :func:`encrypt_secret` → the original JSON value."""
    data = bytes.fromhex(encrypted_hex)
    pt = AESGCM(_key()).decrypt(data[:_NONCE_LEN], data[_NONCE_LEN:], None)
    return json.loads(pt.decode("utf-8"))
