"""Webhook signature verification (ORAA-4 §21 domain layer) — pure, no I/O.

Generic scheme (R6 Slice 7 v1): HMAC-SHA256 over the EXACT raw request bytes, presented as
``sha256=<hex>`` (the X-Hub-Signature-256 convention). Constant-time compare; fail-closed on any
absent/malformed/mismatched signature. The subscription PINS its scheme, so a scheme is never
inferred from request headers (no downgrade attack); per-provider schemes (GitHub/Stripe/Slack) are
a recorded follow-on built on this same pure seam.
"""

from __future__ import annotations

import hashlib
import hmac

GENERIC = "generic"
_PREFIX = "sha256="


def verify_generic_hmac(*, secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """True iff ``signature_header`` is ``sha256=<hex>`` and the hex equals
    HMAC-SHA256(secret, raw_body), in constant time. Any absent/malformed header -> False."""
    if not signature_header or not signature_header.startswith(_PREFIX):
        return False
    provided = signature_header[len(_PREFIX) :].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)
