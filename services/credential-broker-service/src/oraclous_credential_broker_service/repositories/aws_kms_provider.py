"""AWS KMS provider (repositories layer, ADR-020) — the cloud drop-in for the KMS seam.

The CMK never leaves AWS KMS; the broker only ever sees a wrapped DEK + its transiently-unwrapped
plaintext. ``boto3`` is imported lazily so it is NOT a hard dependency of the local-default
deployment — it is required only when ``KMS_PROVIDER=aws`` (the cloud cutover). The blocking boto3
calls run in a thread so they don't stall the event loop. The ``client`` is injectable for tests
(a fake KMS client).
"""

from __future__ import annotations

import asyncio
from typing import Any


class AwsKmsProvider:
    def __init__(
        self, *, key_id: str, region: str | None = None, client: Any | None = None
    ) -> None:
        self._key_id = key_id
        if client is not None:
            self._client = client
        else:
            import boto3  # lazy: only needed when KMS_PROVIDER=aws

            self._client = (
                boto3.client("kms", region_name=region) if region else boto3.client("kms")
            )

    @property
    def key_id(self) -> str:
        return self._key_id

    async def generate_data_key(self) -> tuple[bytes, bytes]:
        resp = await asyncio.to_thread(
            self._client.generate_data_key, KeyId=self._key_id, KeySpec="AES_256"
        )
        return resp["Plaintext"], resp["CiphertextBlob"]

    async def decrypt_data_key(self, wrapped_dek: bytes) -> bytes:
        resp = await asyncio.to_thread(
            self._client.decrypt, CiphertextBlob=wrapped_dek, KeyId=self._key_id
        )
        return resp["Plaintext"]
