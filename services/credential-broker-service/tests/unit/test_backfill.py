"""Unit: the envelope backfill re-encrypts v1 → v2, skips v2, idempotent (ADR-020 §3, S5)."""

from __future__ import annotations

import base64
import os
import uuid

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _enc_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "k")
    from oraclous_credential_broker_service.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeCredRepo:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self._rows = rows
        self.updated: dict = {}

    async def iter_all_ciphertexts(self):  # noqa: ANN202
        return self._rows

    async def set_encrypted_cred(self, *, cred_id, encrypted_cred):  # noqa: ANN001, ANN202
        self.updated[cred_id] = encrypted_cred


class _FakeWhRepo:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self._rows = rows
        self.updated: dict = {}

    async def iter_all_ciphertexts(self):  # noqa: ANN202
        return self._rows

    async def set_encrypted_secret(self, *, secret_id, encrypted_secret):  # noqa: ANN001, ANN202
        self.updated[secret_id] = encrypted_secret


async def test_backfill_rewraps_v1_and_skips_v2(test_envelope) -> None:  # noqa: ANN001
    from oraclous_credential_broker_service.core.envelope import is_v2
    from oraclous_credential_broker_service.core.security import encrypt_secret
    from oraclous_credential_broker_service.services.backfill_service import BackfillService

    org = uuid.uuid4()
    c_v1, c_v2 = uuid.uuid4(), uuid.uuid4()
    legacy = encrypt_secret({"t": "legacy"})  # a real v1 ciphertext
    already = await test_envelope.encrypt(organisation_id=org, plaintext={"t": "already"})  # v2
    creds = _FakeCredRepo([(c_v1, org, legacy), (c_v2, org, already)])
    whs = _FakeWhRepo([])

    result = await BackfillService(
        envelope=test_envelope, credentials=creds, webhook_secrets=whs
    ).run()

    assert result == {"credentials": 1, "webhook_secrets": 0}  # only the one v1 row rewrapped
    assert c_v1 in creds.updated and is_v2(creds.updated[c_v1])  # now enveloped
    assert c_v2 not in creds.updated  # the v2 row was skipped (idempotent)
    # the rewrapped value still decrypts to the original plaintext
    assert await test_envelope.decrypt(organisation_id=org, stored=creds.updated[c_v1]) == {
        "t": "legacy"
    }


async def test_backfill_is_a_noop_when_all_v2(test_envelope) -> None:  # noqa: ANN001
    from oraclous_credential_broker_service.services.backfill_service import BackfillService

    org = uuid.uuid4()
    sid = uuid.uuid4()
    v2 = await test_envelope.encrypt(organisation_id=org, plaintext="s")
    whs = _FakeWhRepo([(sid, org, v2)])
    result = await BackfillService(
        envelope=test_envelope, credentials=_FakeCredRepo([]), webhook_secrets=whs
    ).run()
    assert result == {"credentials": 0, "webhook_secrets": 0}
    assert whs.updated == {}  # nothing rewrapped — safe to re-run


async def test_backfill_bad_row_is_swallowed_but_visible(test_envelope) -> None:  # noqa: ANN001
    """ADR-021 §1 / #296: a single un-rewrappable row no longer aborts the resumable sweep silently
    — it emits a structured ``envelope_backfill_row_failed`` alert and the loop CONTINUES, so a
    stalled backfill surfaces to ops while the good rows still rewrap."""
    from oraclous_credential_broker_service.core.security import encrypt_secret
    from oraclous_credential_broker_service.services.backfill_service import BackfillService
    from oraclous_telemetry import DegradationEvent, register_sink, reset_sinks

    events: list[DegradationEvent] = []
    reset_sinks()
    register_sink(events.append)
    try:
        org = uuid.uuid4()
        good_id, bad_id = uuid.uuid4(), uuid.uuid4()
        good = encrypt_secret({"t": "good"})
        creds = _FakeCredRepo([(bad_id, org, "not-a-ciphertext"), (good_id, org, good)])
        result = await BackfillService(
            envelope=test_envelope, credentials=creds, webhook_secrets=_FakeWhRepo([])
        ).run()
        assert result["credentials"] == 1  # the good row rewrapped; the bad one was skipped
        assert good_id in creds.updated and bad_id not in creds.updated
        fired = [e for e in events if e.code == "envelope_backfill_row_failed"]
        assert len(fired) == 1
        assert fired[0].context["table"] == "credentials"
        assert fired[0].context["row_id"] == str(bad_id)
    finally:
        reset_sinks()
