"""Unit: the pure generic HMAC-SHA256 webhook verifier (R6 Slice 7). No I/O."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from oraclous_application_gateway_service.domain.webhook_signature import verify_generic_hmac

pytestmark = pytest.mark.unit

_SECRET = "whsec_test"  # noqa: S105 — test fixture
_BODY = b'{"event":"push","n":1}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_correct_signature_verifies() -> None:
    assert verify_generic_hmac(
        secret=_SECRET, raw_body=_BODY, signature_header=_sign(_SECRET, _BODY)
    )


def test_a_tampered_body_fails() -> None:
    assert not verify_generic_hmac(
        secret=_SECRET, raw_body=_BODY + b"x", signature_header=_sign(_SECRET, _BODY)
    )


def test_the_wrong_secret_fails() -> None:
    assert not verify_generic_hmac(
        secret="other",  # noqa: S106
        raw_body=_BODY,
        signature_header=_sign(_SECRET, _BODY),
    )


def test_an_absent_header_fails() -> None:
    assert not verify_generic_hmac(secret=_SECRET, raw_body=_BODY, signature_header=None)


def test_a_malformed_header_fails() -> None:
    # no sha256= prefix, and a bare hex without the scheme
    assert not verify_generic_hmac(secret=_SECRET, raw_body=_BODY, signature_header="deadbeef")
    assert not verify_generic_hmac(secret=_SECRET, raw_body=_BODY, signature_header="md5=abc")
