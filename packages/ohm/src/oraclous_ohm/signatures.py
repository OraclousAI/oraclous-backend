"""OHM signature verification (ORAA-4 §21 domain layer; OHM v1.0 spec §2/§7).

Verifies the ``signatures`` block against a configured **trust store** (signer-id → public key PEM).
The signed bytes are the OHM's canonical, signature-excluded form (see ``canonical.py``); each entry
is ``{signer, algorithm, signature}`` where ``signature`` is base64 of the raw public-key signature.

Scheme (pinned for v1):
- ``EdDSA`` → Ed25519 over the canonical bytes (no pre-hash).
- ``ES256`` → ECDSA P-256 / SHA-256, DER-encoded signature.
- ``RS256`` → RSA PKCS#1 v1.5 / SHA-256.

Fail-closed: an unknown signer, an unsupported algorithm, or an invalid signature raises
``OHMSignatureError``. An OHM with **no** signatures verifies trivially — whether a signature is
*required* is a governance decision (slice 3), not a parse-time one. ``make_signature`` is the
inverse, used by tests + the smoke to produce a valid entry.
"""

from __future__ import annotations

import base64
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes, PublicKeyTypes

from oraclous_ohm.canonical import canonical_bytes
from oraclous_ohm.errors import OHMSignatureError

_SUPPORTED = {"EdDSA", "ES256", "RS256"}


class TrustStore:
    """Signer-id → public key. Built from config (a map of signer-id → PEM)."""

    def __init__(self, keys_pem: dict[str, str] | None = None) -> None:
        self._keys: dict[str, PublicKeyTypes] = {}
        for signer, pem in (keys_pem or {}).items():
            self._keys[signer] = serialization.load_pem_public_key(pem.encode("utf-8"))

    def get(self, signer: str) -> PublicKeyTypes | None:
        return self._keys.get(signer)

    def __len__(self) -> int:
        return len(self._keys)


def _verify_one(public_key: PublicKeyTypes, algorithm: str, signature: bytes, data: bytes) -> None:
    if algorithm == "EdDSA":
        if not isinstance(public_key, ed25519.Ed25519PublicKey):
            raise OHMSignatureError("EdDSA signature requires an Ed25519 key")
        public_key.verify(signature, data)
    elif algorithm == "ES256":
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise OHMSignatureError("ES256 signature requires an EC key")
        public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    elif algorithm == "RS256":
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise OHMSignatureError("RS256 signature requires an RSA key")
        public_key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    else:  # pragma: no cover — guarded by the caller
        raise OHMSignatureError(f"unsupported signature algorithm {algorithm!r}")


def verify_signatures(
    document: dict[str, Any], trust: TrustStore, *, require: bool = False
) -> None:
    """Verify every signature on the OHM vs the trust store (fail-closed). When ``require`` is set,
    an unsigned OHM is rejected (closes signature-stripping); else unsigned verifies trivially."""
    entries = document.get("signatures") or []
    if require and not entries:
        raise OHMSignatureError("a signed OHM is required but none was provided")
    data = canonical_bytes(document)
    for entry in entries:
        signer = entry.get("signer")
        algorithm = entry.get("algorithm")
        raw = entry.get("signature")
        if not signer or not raw:
            raise OHMSignatureError("signature entry missing 'signer' or 'signature'")
        if algorithm not in _SUPPORTED:
            raise OHMSignatureError(f"unsupported signature algorithm {algorithm!r}")
        public_key = trust.get(signer)
        if public_key is None:
            raise OHMSignatureError(f"unknown signer {signer!r} (no trusted key)")
        try:
            _verify_one(public_key, algorithm, base64.b64decode(raw), data)
        except InvalidSignature as exc:
            raise OHMSignatureError(f"invalid signature from {signer!r}") from exc
        except (ValueError, TypeError) as exc:
            raise OHMSignatureError(f"malformed signature from {signer!r}: {exc}") from exc


def make_signature(
    document: dict[str, Any], *, signer: str, algorithm: str, private_key: PrivateKeyTypes
) -> dict[str, str]:
    """Produce a valid ``signatures`` entry for a document (test/smoke helper)."""
    data = canonical_bytes(document)
    # ``PrivateKeyTypes`` is a union whose members' ``.sign()`` signatures diverge (Ed25519 takes
    # only the data; EC takes a signature algorithm; RSA takes padding + a hash). Narrow to the
    # concrete key type per algorithm (control flow) so the right overload resolves — and so a
    # key/algorithm mismatch fails closed, mirroring ``_verify_one``'s key-type guards.
    if algorithm == "EdDSA":
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise OHMSignatureError("EdDSA signature requires an Ed25519 key")
        sig = private_key.sign(data)
    elif algorithm == "ES256":
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise OHMSignatureError("ES256 signature requires an EC key")
        sig = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    elif algorithm == "RS256":
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise OHMSignatureError("RS256 signature requires an RSA key")
        sig = private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise OHMSignatureError(f"unsupported signature algorithm {algorithm!r}")
    return {"signer": signer, "algorithm": algorithm, "signature": base64.b64encode(sig).decode()}
