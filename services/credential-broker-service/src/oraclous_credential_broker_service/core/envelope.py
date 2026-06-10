"""Per-org envelope cipher primitives + the local KMS provider (ORAA-4 §21 core layer, ADR-020).

The versioned ciphertext lets the single-key (v1) and envelope (v2) formats coexist during the
online migration:

* **v1** — the legacy ``hex(nonce‖ct)`` under the single ``ENCRYPTION_KEY`` (``core/security``);
  untagged.
* **v2** — ``"v2:" + base64(nonce‖ct)`` under the org's DEK, with the ``organisation_id`` bound as
  AEAD associated data (a v2 ciphertext is cryptographically pinned to its org — it cannot be
  replayed under another org's DEK even if a DEK were ever shared).

``LocalKmsProvider`` is the env-KEK default (dev / self-host / pre-cutover cloud): a 32-byte base64
KEK wraps each DEK with AES-256-GCM. ``AwsKmsProvider`` (repositories layer) is the cloud drop-in.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_V2_PREFIX = "v2:"
_NONCE_LEN = 12
_DEK_LEN = 32
_LOCAL_KEK_INFO = b"oraclous-kms-local-kek-v2"  # HKDF domain-separation label


def derive_local_kek(encryption_key_b64: str) -> str:
    """Derive the local KEK from the legacy ``ENCRYPTION_KEY`` via HKDF-SHA256 with a domain label
    (ADR-020 hardening). This keeps the KEK role cryptographically separate from the v1 data role
    even when both come from the same env value — so the wrap key and the secret key never share raw
    bytes. Returns a base64 32-byte KEK. (Setting ``KMS_LOCAL_KEK`` explicitly bypasses this.)"""
    raw = base64.b64decode(encryption_key_b64)
    derived = HKDF(
        algorithm=hashes.SHA256(), length=_DEK_LEN, salt=None, info=_LOCAL_KEK_INFO
    ).derive(raw)
    return base64.b64encode(derived).decode("ascii")


def is_v2(stored: str) -> bool:
    """True if ``stored`` is an envelope (v2) ciphertext; anything else is legacy v1."""
    return stored.startswith(_V2_PREFIX)


def encrypt_with_dek(dek: bytes, *, organisation_id: str, plaintext: Any) -> str:
    """Encrypt a JSON-serialisable secret under a 32-byte DEK → a ``v2:`` ciphertext, binding the
    org as AEAD associated data."""
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(dek).encrypt(nonce, json.dumps(plaintext).encode("utf-8"), organisation_id.encode())
    return _V2_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_with_dek(dek: bytes, *, organisation_id: str, stored: str) -> Any:
    """Inverse of :func:`encrypt_with_dek`. The org AAD must match (else GCM auth fails,
    fail-closed)."""
    raw = base64.b64decode(stored[len(_V2_PREFIX) :])
    pt = AESGCM(dek).decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], organisation_id.encode())
    return json.loads(pt.decode("utf-8"))


class LocalKmsProvider:
    """KEK held in the process env (base64, 32 bytes). Wraps/unwraps DEKs with AES-256-GCM. The
    operator still holds the KEK (self-host stays sovereign); the per-org DEK structure + the
    migration are identical to the AWS path, so the cutover is a config flip, not a rewrite."""

    def __init__(self, kek_b64: str) -> None:
        self._kek = base64.b64decode(kek_b64)
        if len(self._kek) != _DEK_LEN:
            raise ValueError("KMS_LOCAL_KEK must decode to 32 bytes (AES-256)")

    @property
    def key_id(self) -> str:
        return "local"

    async def generate_data_key(self) -> tuple[bytes, bytes]:
        dek = os.urandom(_DEK_LEN)
        nonce = os.urandom(_NONCE_LEN)
        wrapped = nonce + AESGCM(self._kek).encrypt(nonce, dek, None)
        return dek, wrapped

    async def decrypt_data_key(self, wrapped_dek: bytes) -> bytes:
        return AESGCM(self._kek).decrypt(wrapped_dek[:_NONCE_LEN], wrapped_dek[_NONCE_LEN:], None)
