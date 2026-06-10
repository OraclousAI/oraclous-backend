"""Unit: the pure webhook verifiers — generic/github/stripe/slack + the dispatcher. No I/O."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from oraclous_application_gateway_service.domain.webhook_signature import (
    GENERIC,
    GITHUB,
    SLACK,
    STRIPE,
    verify_generic_hmac,
    verify_signature,
)

pytestmark = pytest.mark.unit

_SECRET = "whsec_test"  # noqa: S105 — test fixture
_BODY = b'{"event":"push","n":1}'
_NOW = 1_700_000_000


def _hex(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + _hex(secret, body)


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


# --- the pinned dispatcher + per-provider schemes (WH-1) -----------------------------------------
def _v(scheme: str, headers: dict, *, body: bytes = _BODY, now: int = _NOW) -> bool:
    return verify_signature(scheme, secret=_SECRET, raw_body=body, headers=headers, now_unix=now)


def test_generic_and_github_use_the_x_hub_header() -> None:
    hdr = {"x-hub-signature-256": _sign(_SECRET, _BODY)}
    assert _v(GENERIC, hdr) and _v(GITHUB, hdr)
    assert not _v(GENERIC, hdr, body=_BODY + b"x")  # tampered body
    assert not _v(GENERIC, {})  # absent header


def test_stripe_verifies_replays_and_rotated_secrets() -> None:
    sig = _hex(_SECRET, f"{_NOW}.".encode() + _BODY)  # Stripe signs "<t>.<body>"
    assert _v(STRIPE, {"stripe-signature": f"t={_NOW},v1={sig}"})
    # a far-off timestamp is rejected (replay window)
    assert not _v(STRIPE, {"stripe-signature": f"t={_NOW},v1={sig}"}, now=_NOW + 10_000)
    # rotating secret: multiple v1, one valid -> passes
    assert _v(STRIPE, {"stripe-signature": f"t={_NOW},v1=deadbeef,v1={sig}"})
    # tampered body / missing parts -> fail closed
    assert not _v(STRIPE, {"stripe-signature": f"t={_NOW},v1={sig}"}, body=_BODY + b"x")
    assert not _v(STRIPE, {"stripe-signature": f"v1={sig}"})  # no t
    assert not _v(STRIPE, {})


def test_slack_verifies_and_enforces_the_replay_window() -> None:
    base = b"v0:" + str(_NOW).encode() + b":" + _BODY
    sig = "v0=" + _hex(_SECRET, base)
    hdr = {"x-slack-signature": sig, "x-slack-request-timestamp": str(_NOW)}
    assert _v(SLACK, hdr)
    assert not _v(SLACK, hdr, now=_NOW + 10_000)  # replay
    assert not _v(SLACK, {"x-slack-signature": sig})  # missing timestamp
    assert not _v(SLACK, hdr, body=_BODY + b"x")  # tampered body


def test_an_unknown_scheme_fails_closed() -> None:
    # a stored-but-unknown scheme never falls through to a weaker check
    assert not _v("hmac-but-trust-me", {"x-hub-signature-256": _sign(_SECRET, _BODY)})


def test_a_scheme_does_not_accept_another_schemes_signature() -> None:
    # a valid generic signature must NOT verify under the stripe scheme (no cross-scheme acceptance)
    assert not _v(STRIPE, {"x-hub-signature-256": _sign(_SECRET, _BODY)})
