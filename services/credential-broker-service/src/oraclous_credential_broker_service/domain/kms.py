"""KMS provider seam (domain layer, ADR-020) — the narrow interface the envelope service
depends on, so the per-org DEK lifecycle is independent of *where* the KEK lives.

A KEK (key-encryption key) wraps a per-org DEK (data-encryption key). ``generate_data_key`` mints a
fresh DEK + its KEK-wrapped form (the plaintext is returned ONCE, never persisted);
``decrypt_data_key`` unwraps a stored wrap. ``LocalKmsProvider`` (the env-KEK default) and
``AwsKmsProvider`` (a CMK in AWS KMS) both satisfy this; the broker imports no concrete provider.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KmsProvider(Protocol):
    @property
    def key_id(self) -> str:
        """The KEK identifier recorded on the org_data_keys row, so a wrap can be traced to its
        KEK after a rotation."""
        ...

    async def generate_data_key(self) -> tuple[bytes, bytes]:
        """Return ``(plaintext_dek, wrapped_dek)`` — a fresh 32-byte DEK + its KEK wrap. The
        plaintext is the caller's to use transiently and never persist; only the wrap is stored."""
        ...

    async def decrypt_data_key(self, wrapped_dek: bytes) -> bytes:
        """Unwrap a stored ``wrapped_dek`` back to the 32-byte plaintext DEK."""
        ...
