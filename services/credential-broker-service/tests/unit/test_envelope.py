"""Unit: the per-org envelope — codec, LocalKms, AwsKms, polymorphic service (R7-SEC S5)."""

from __future__ import annotations

import base64
import os
import uuid

import pytest
from oraclous_credential_broker_service.core.envelope import (
    LocalKmsProvider,
    decrypt_with_dek,
    derive_local_kek,
    encrypt_with_dek,
    is_v2,
)
from oraclous_credential_broker_service.services.envelope_service import EnvelopeService

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_OTHER = uuid.uuid4()


# --- codec ---------------------------------------------------------------------------------------
def test_v2_roundtrip_and_tag() -> None:
    dek = os.urandom(32)
    ct = encrypt_with_dek(dek, organisation_id=str(_ORG), plaintext={"k": "v"})
    assert is_v2(ct) and ct.startswith("v2:")
    assert decrypt_with_dek(dek, organisation_id=str(_ORG), stored=ct) == {"k": "v"}


def test_org_aad_pins_the_ciphertext_to_its_org() -> None:
    # a v2 ciphertext for org A cannot be decrypted with B's org as AAD (GCM auth fails)
    dek = os.urandom(32)
    ct = encrypt_with_dek(dek, organisation_id=str(_ORG), plaintext="secret")
    with pytest.raises(Exception):  # noqa: B017, PT011 — InvalidTag (cryptography)
        decrypt_with_dek(dek, organisation_id=str(_OTHER), stored=ct)


def test_only_a_v2_prefix_is_treated_as_envelope() -> None:
    # an untagged legacy hex value is v1; only the "v2:" prefix marks the envelope format
    assert is_v2("deadbeef0123legacyhex") is False
    assert is_v2("v2:AAAA") is True


# --- LocalKmsProvider ----------------------------------------------------------------------------
async def test_local_kms_wrap_unwrap_roundtrip() -> None:
    kms = LocalKmsProvider(base64.b64encode(os.urandom(32)).decode())
    dek, wrapped = await kms.generate_data_key()
    assert len(dek) == 32 and wrapped != dek
    assert await kms.decrypt_data_key(wrapped) == dek
    assert kms.key_id == "local"


def test_local_kms_rejects_a_bad_kek() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        LocalKmsProvider(base64.b64encode(os.urandom(16)).decode())


def test_derived_local_kek_is_deterministic_and_separated_from_the_data_key() -> None:
    enc = base64.b64encode(os.urandom(32)).decode()
    k1 = derive_local_kek(enc)
    assert k1 == derive_local_kek(enc)  # deterministic — the same env yields the same KEK
    assert base64.b64decode(k1) != base64.b64decode(
        enc
    )  # the KEK never shares the raw data-key bytes
    assert len(base64.b64decode(k1)) == 32


# --- AwsKmsProvider (injected fake client) -------------------------------------------------------
class _FakeKmsClient:
    def __init__(self) -> None:
        self.gen_calls = 0

    def generate_data_key(self, *, KeyId, KeySpec):  # noqa: N803, ARG002
        self.gen_calls += 1
        return {"Plaintext": b"D" * 32, "CiphertextBlob": b"wrapped-blob"}

    def decrypt(self, *, CiphertextBlob, KeyId):  # noqa: N803, ARG002
        assert CiphertextBlob == b"wrapped-blob"
        return {"Plaintext": b"D" * 32}


async def test_aws_kms_provider_uses_the_cmk_via_the_client() -> None:
    from oraclous_credential_broker_service.repositories.aws_kms_provider import AwsKmsProvider

    fake = _FakeKmsClient()
    kms = AwsKmsProvider(key_id="arn:aws:kms:...:key/abc", client=fake)
    dek, wrapped = await kms.generate_data_key()
    assert dek == b"D" * 32 and wrapped == b"wrapped-blob" and fake.gen_calls == 1
    assert await kms.decrypt_data_key(wrapped) == b"D" * 32
    assert kms.key_id == "arn:aws:kms:...:key/abc"


# --- EnvelopeService (the heart) -----------------------------------------------------------------
class _MemDekRepo:
    def __init__(self) -> None:
        self.rows: dict = {}
        self.creates = 0

    async def get_for_org(self, *, organisation_id):  # noqa: ANN001, ANN202
        return self.rows.get(organisation_id)

    async def create(self, *, organisation_id, wrapped_dek, kek_provider, kek_key_id):  # noqa: ANN001, ANN202
        self.creates += 1
        from types import SimpleNamespace

        row = SimpleNamespace(
            organisation_id=organisation_id,
            wrapped_dek=wrapped_dek,
            kek_provider=kek_provider,
            kek_key_id=kek_key_id,
        )
        self.rows[organisation_id] = row
        return row


def _envelope(dek_repo=None):  # noqa: ANN001, ANN202
    return EnvelopeService(
        kms=LocalKmsProvider(base64.b64encode(os.urandom(32)).decode()),
        dek_repo=dek_repo or _MemDekRepo(),
        legacy_decrypt=lambda stored: {"v1": stored},  # a sentinel so we can see the v1 path taken
    )


async def test_encrypt_writes_v2_and_roundtrips() -> None:
    env = _envelope()
    ct = await env.encrypt(organisation_id=_ORG, plaintext={"token": "abc"})
    assert is_v2(ct)
    assert await env.decrypt(organisation_id=_ORG, stored=ct) == {"token": "abc"}


async def test_decrypt_is_polymorphic_v1_falls_back_to_legacy() -> None:
    env = _envelope()
    # an untagged (v1) value never touches the DEK — it goes to the legacy decrypt
    assert await env.decrypt(organisation_id=_ORG, stored="deadbeef-legacy-hex") == {
        "v1": "deadbeef-legacy-hex"
    }


async def test_dek_is_created_once_per_org_then_cached() -> None:
    repo = _MemDekRepo()
    env = _envelope(repo)
    await env.encrypt(organisation_id=_ORG, plaintext="a")
    await env.encrypt(organisation_id=_ORG, plaintext="b")
    await env.decrypt(
        organisation_id=_ORG,
        stored=await env.encrypt(organisation_id=_ORG, plaintext="c"),
    )
    assert repo.creates == 1  # one lazy create; subsequent ops hit the cache / the stored row


async def test_a_v2_ciphertext_for_one_org_does_not_decrypt_under_another() -> None:
    repo = _MemDekRepo()
    env = _envelope(repo)
    ct_a = await env.encrypt(organisation_id=_ORG, plaintext="orgA-secret")
    # org B has its own DEK; decrypting A's ciphertext under B fails closed (wrong DEK + AAD)
    with pytest.raises(Exception):  # noqa: B017, PT011
        await env.decrypt(organisation_id=_OTHER, stored=ct_a)
