"""Integration: per-org KMS envelope round-trip vs real Postgres (ADR-020, #233).

The envelope's unit suite drives an in-MEMORY DEK store. This exercises the SAME ``EnvelopeService``
end-to-end against the REAL ``OrgDataKeyRepository`` + real Postgres under local-KMS mode:
  - encrypt lazily MINTS + PERSISTS the org's wrapped DEK (a real ``org_data_keys`` row) and emits a
    ``v2:`` envelope; the ciphertext is not the plaintext;
  - a brand-new ``EnvelopeService`` (cold in-process cache) RE-READS that persisted wrap from
    Postgres, unwraps the DEK via local-KMS, and decrypts the value — proving the wrap actually
    survives a DB round-trip;
  - the DEK is created exactly ONCE per org (the row is reused on subsequent writes);
  - a v2 ciphertext minted for org A fails closed when decrypted under org B's DEK + AAD.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from oraclous_credential_broker_service.core.envelope import LocalKmsProvider, is_v2
from oraclous_credential_broker_service.core.security import decrypt_secret
from oraclous_credential_broker_service.repositories.org_data_key_repository import (
    OrgDataKeyRepository,
)
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService

pytestmark = pytest.mark.integration

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000aaaa")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000bbbb")


@pytest.fixture
async def deks(postgres_dsn: str) -> AsyncIterator[OrgDataKeyRepository]:
    """A real ``OrgDataKeyRepository`` against a freshly-migrated test Postgres."""
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    from oraclous_credential_broker_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    repo = OrgDataKeyRepository(async_dsn)
    yield repo
    await repo.close()


def _envelope(repo: OrgDataKeyRepository, kek: str) -> EnvelopeService:
    """An EnvelopeService in local-KMS mode bound to the real DEK repo (shared KEK across calls)."""
    return EnvelopeService(kms=LocalKmsProvider(kek), dek_repo=repo, legacy_decrypt=decrypt_secret)


def _kek() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


async def test_encrypt_persists_dek_and_roundtrips(deks: OrgDataKeyRepository) -> None:
    kek = _kek()
    env = _envelope(deks, kek)
    secret = {"token": "s3cr3t", "n": 1}

    ct = await env.encrypt(organisation_id=_ORG_A, plaintext=secret)
    assert is_v2(ct) and ct.startswith("v2:")
    assert ct != str(secret)  # ciphertext is not the plaintext

    # a real wrapped-DEK row landed in Postgres for this org
    row = await deks.get_for_org(organisation_id=_ORG_A)
    assert row is not None and row.kek_provider == "local"
    assert base64.b64decode(row.wrapped_dek)  # stored base64, decodable

    # same service round-trips
    assert await env.decrypt(organisation_id=_ORG_A, stored=ct) == secret


async def test_decrypt_after_cold_reread_from_postgres(deks: OrgDataKeyRepository) -> None:
    kek = _kek()
    secret = "a-bearer-token"  # noqa: S105 — test plaintext to encrypt, not a real credential
    ct = await _envelope(deks, kek).encrypt(organisation_id=_ORG_A, plaintext=secret)

    # a FRESH EnvelopeService (empty in-process DEK cache, same KEK) must re-read the wrapped DEK
    # from Postgres, unwrap it via local-KMS, and decrypt — proving the persisted wrap is usable.
    fresh = _envelope(deks, kek)
    assert await fresh.decrypt(organisation_id=_ORG_A, stored=ct) == secret


async def test_dek_is_minted_once_per_org(deks: OrgDataKeyRepository) -> None:
    env = _envelope(deks, _kek())
    await env.encrypt(organisation_id=_ORG_A, plaintext="a")
    row1 = await deks.get_for_org(organisation_id=_ORG_A)
    await env.encrypt(organisation_id=_ORG_A, plaintext="b")
    row2 = await deks.get_for_org(organisation_id=_ORG_A)
    assert row1 is not None and row2 is not None
    assert row1.id == row2.id  # the same persisted DEK row is reused, not re-minted


async def test_v2_for_one_org_fails_closed_under_another(deks: OrgDataKeyRepository) -> None:
    env = _envelope(deks, _kek())
    ct_a = await env.encrypt(organisation_id=_ORG_A, plaintext="orgA-only")
    # org B lazily gets its OWN distinct DEK; A's ciphertext fails closed under B (wrong DEK + AAD)
    with pytest.raises(Exception):  # noqa: B017, PT011 — InvalidTag (cryptography)
        await env.decrypt(organisation_id=_ORG_B, stored=ct_a)
