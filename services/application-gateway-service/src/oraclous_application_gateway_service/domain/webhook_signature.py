"""Webhook signature verification (ORAA-4 §21 domain layer) — pure, no I/O.

The subscription PINS its scheme, so a scheme is never inferred from request headers (no downgrade
attack). Constant-time compare; fail-closed on any absent/malformed/mismatched signature, and on an
unknown scheme. Schemes:

* ``generic`` / ``github`` — HMAC-SHA256 over the EXACT raw bytes, ``X-Hub-Signature-256: sha256=…``
  (GitHub's convention; ``generic`` is the same wire).
* ``stripe`` — ``Stripe-Signature: t=<ts>,v1=<hex>[,…]``; HMAC-SHA256 over ``<ts>.<body>``,
  with a ±5-min replay window on ``<ts>`` (rotating-secret tolerance → multiple ``v1``).
* ``slack`` — ``X-Slack-Signature: v0=<hex>`` over ``v0:<X-Slack-Request-Timestamp>:<body>``
  (the same ±5-min replay window).

The timestamped schemes need a clock — the caller passes ``now_unix`` (this layer stays pure).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

GENERIC = "generic"
GITHUB = "github"
STRIPE = "stripe"
SLACK = "slack"
SCHEMES = frozenset({GENERIC, GITHUB, STRIPE, SLACK})

_PREFIX = "sha256="
_REPLAY_TOLERANCE_S = 300  # reject a timestamped signature more than 5 min from now (anti-replay)


def verify_generic_hmac(*, secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """True iff ``signature_header`` is ``sha256=<hex>`` and the hex equals
    HMAC-SHA256(secret, raw_body), in constant time. Any absent/malformed header -> False."""
    if not signature_header or not signature_header.startswith(_PREFIX):
        return False
    provided = signature_header[len(_PREFIX) :].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def _verify_stripe(
    *, secret: str, raw_body: bytes, headers: Mapping[str, str], now_unix: int
) -> bool:
    header = headers.get("stripe-signature")
    if not header:
        return False
    ts: str | None = None
    sigs: list[str] = []
    for item in header.split(","):
        key, _, value = item.partition("=")
        key = key.strip()
        if key == "t":
            ts = value.strip()
        elif key == "v1":
            sigs.append(value.strip())
    if ts is None or not sigs:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(now_unix - ts_int) > _REPLAY_TOLERANCE_S:
        return False
    signed = ts.encode("ascii") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def _verify_slack(
    *, secret: str, raw_body: bytes, headers: Mapping[str, str], now_unix: int
) -> bool:
    sig = headers.get("x-slack-signature")
    ts = headers.get("x-slack-request-timestamp")
    if not sig or not ts or not sig.startswith("v0="):
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(now_unix - ts_int) > _REPLAY_TOLERANCE_S:
        return False
    basestring = b"v0:" + ts.encode("ascii") + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def verify_signature(
    scheme: str, *, secret: str, raw_body: bytes, headers: Mapping[str, str], now_unix: int
) -> bool:
    """Verify an inbound signature against the subscription's PINNED scheme. Fail-closed: an unknown
    scheme rejects (never falls through to a weaker check)."""
    if scheme == STRIPE:
        return _verify_stripe(secret=secret, raw_body=raw_body, headers=headers, now_unix=now_unix)
    if scheme == SLACK:
        return _verify_slack(secret=secret, raw_body=raw_body, headers=headers, now_unix=now_unix)
    if scheme in (GENERIC, GITHUB):
        return verify_generic_hmac(
            secret=secret, raw_body=raw_body, signature_header=headers.get("x-hub-signature-256")
        )
    return False
