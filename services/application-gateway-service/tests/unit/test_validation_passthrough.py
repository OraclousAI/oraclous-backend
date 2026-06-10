"""Unit: leak-safe 422 extraction — adversarial loc/type inputs (#225)."""

from __future__ import annotations

from oraclous_application_gateway_service.domain.validation_passthrough import (
    extract_validation_details,
)
from tools.contract.error_envelope import scan_forbidden


def _raw(detail: object) -> bytes:
    import json

    return json.dumps({"detail": detail}).encode()


def test_dict_key_value_in_loc_is_neutralised() -> None:
    # a user-controlled dict key (email / internal host) in loc must not survive verbatim
    out = extract_validation_details(
        _raw(
            [
                {"loc": ["body", "meta", "secret@corp.internal"], "type": "int_parsing"},
                {"loc": ["body", "host", "db-1.svc.cluster.local"], "type": "missing"},
            ]
        )
    )
    assert out is not None
    blob = " ".join(f"{d.field} {d.issue}" for d in out)
    assert "secret@corp.internal" not in blob and "db-1.svc.cluster.local" not in blob
    assert "@" not in blob and scan_forbidden(blob) == []
    # every emitted field/issue is contract-conformant (no raw value)
    for d in out:
        assert d.issue.isupper() and all(c.isalnum() or c == "_" for c in d.issue)


def test_type_reflecting_a_value_is_neutralised() -> None:
    out = extract_validation_details(_raw([{"loc": ["body", "x"], "type": "alice@corp.internal"}]))
    assert out is not None
    assert "alice@corp.internal" not in out[0].issue and "@" not in out[0].issue


def test_string_detail_returns_none() -> None:
    assert extract_validation_details(_raw("free text on 10.0.0.5")) is None


def test_oversized_body_returns_none() -> None:
    assert extract_validation_details(b"x" * (64 * 1024 + 1)) is None
