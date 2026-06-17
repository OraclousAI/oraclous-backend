"""OHM signature verification (slice 2): all three algorithms + fail-closed paths. No DB/network."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from oraclous_harness_runtime_service.domain.ohm.errors import OHMSignatureError
from oraclous_harness_runtime_service.domain.ohm.signatures import (
    TrustStore,
    make_signature,
    verify_signatures,
)

pytestmark = [pytest.mark.unit, pytest.mark.ohm_signature]

_DOC = {"ohm_version": "1.0", "metadata": {"name": "X"}, "runtime": {"entrypoint": "pg"}}


def _pub_pem(public_key) -> str:  # noqa: ANN001
    return public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def _signed(doc: dict, *, signer: str, algorithm: str, private_key) -> dict:  # noqa: ANN001
    return {
        **doc,
        "signatures": [
            make_signature(doc, signer=signer, algorithm=algorithm, private_key=private_key)
        ],
    }


def test_ed25519_valid_signature_verifies() -> None:
    priv = ed25519.Ed25519PrivateKey.generate()
    trust = TrustStore({"k1": _pub_pem(priv.public_key())})
    verify_signatures(_signed(_DOC, signer="k1", algorithm="EdDSA", private_key=priv), trust)


def test_es256_valid_signature_verifies() -> None:
    priv = ec.generate_private_key(ec.SECP256R1())
    trust = TrustStore({"k2": _pub_pem(priv.public_key())})
    verify_signatures(_signed(_DOC, signer="k2", algorithm="ES256", private_key=priv), trust)


def test_rs256_valid_signature_verifies() -> None:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    trust = TrustStore({"k3": _pub_pem(priv.public_key())})
    verify_signatures(_signed(_DOC, signer="k3", algorithm="RS256", private_key=priv), trust)


def test_unsigned_document_is_allowed() -> None:
    verify_signatures(_DOC, TrustStore({}))  # no signatures → no-op (required-ness is governance)


def test_tampered_document_fails() -> None:
    priv = ed25519.Ed25519PrivateKey.generate()
    trust = TrustStore({"k1": _pub_pem(priv.public_key())})
    signed = _signed(_DOC, signer="k1", algorithm="EdDSA", private_key=priv)
    signed["metadata"] = {"name": "TAMPERED"}  # change content after signing
    with pytest.raises(OHMSignatureError):
        verify_signatures(signed, trust)


def test_unknown_signer_fails() -> None:
    priv = ed25519.Ed25519PrivateKey.generate()
    signed = _signed(_DOC, signer="stranger", algorithm="EdDSA", private_key=priv)
    with pytest.raises(OHMSignatureError):
        verify_signatures(signed, TrustStore({}))  # signer not in the trust store


def test_unsupported_algorithm_fails() -> None:
    priv = ed25519.Ed25519PrivateKey.generate()
    trust = TrustStore({"k1": _pub_pem(priv.public_key())})
    bad = {**_DOC, "signatures": [{"signer": "k1", "algorithm": "HS256", "signature": "zzz"}]}
    with pytest.raises(OHMSignatureError):
        verify_signatures(bad, trust)


def test_key_type_mismatch_fails() -> None:
    ed = ed25519.Ed25519PrivateKey.generate()
    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # claim ES256 but the trusted key is Ed25519 → mismatch
    signed = _signed(_DOC, signer="k1", algorithm="EdDSA", private_key=ed)
    signed["signatures"][0]["algorithm"] = "RS256"
    trust = TrustStore({"k1": _pub_pem(rsa_priv.public_key())})
    with pytest.raises(OHMSignatureError):
        verify_signatures(signed, trust)


def test_require_signature_rejects_unsigned() -> None:
    with pytest.raises(OHMSignatureError):
        verify_signatures(_DOC, TrustStore({}), require=True)  # no signatures + require → reject


def test_require_signature_accepts_validly_signed() -> None:
    priv = ed25519.Ed25519PrivateKey.generate()
    trust = TrustStore({"k1": _pub_pem(priv.public_key())})
    signed = _signed(_DOC, signer="k1", algorithm="EdDSA", private_key=priv)
    verify_signatures(signed, trust, require=True)  # valid signature satisfies the requirement
