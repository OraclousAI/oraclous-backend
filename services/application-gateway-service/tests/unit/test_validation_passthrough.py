"""Unit: leak-safe 422 extraction — adversarial loc/type inputs (#225, #281)."""

from __future__ import annotations

from oraclous_application_gateway_service.domain.validation_passthrough import (
    details_from_errors,
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


# --- #281: details_from_errors (the gateway's OWN RequestValidationError.errors() path) -----------


def test_details_from_errors_field_level_uses_loc_and_type() -> None:
    # a field-level Pydantic error: surface the field path + the machine token, drop the "body" wrap
    out = details_from_errors(
        [{"loc": ["body", "bound_agent_slug"], "type": "string_pattern_mismatch", "msg": "..."}]
    )
    assert out is not None
    assert out[0].field == "bound_agent_slug" and out[0].issue == "STRING_PATTERN_MISMATCH"


def test_details_from_errors_model_level_validator_survives_as_body() -> None:
    # a model_validator (mode="after") error has the bare loc ("body",) — it must still emit ONE
    # detail (field "body") so the request-level XOR rule gives the client field-level feedback
    out = details_from_errors([{"loc": ["body"], "type": "value_error", "msg": "supply one"}])
    assert out is not None
    assert out[0].field == "body" and out[0].issue == "VALUE_ERROR"


def test_details_from_errors_never_reflects_the_msg_value() -> None:
    # the value-reflecting msg/ctx (a CORS origin, an email) must NEVER survive — only loc+type
    out = details_from_errors(
        [
            {
                "loc": ["body", "cors_origins", 0],
                "type": "value_error",
                "msg": "invalid CORS origin 'https://evil.test/path'",
                "ctx": {"error": "https://evil.test/path"},
            }
        ]
    )
    assert out is not None
    blob = " ".join(f"{d.field} {d.issue}" for d in out)
    assert "evil.test" not in blob and "/path" not in blob and scan_forbidden(blob) == []
    assert out[0].field == "cors_origins.0" and out[0].issue == "VALUE_ERROR"


def test_details_from_errors_empty_returns_none() -> None:
    # nothing safe extractable -> None, so the caller falls back to a generic detail
    assert details_from_errors([]) is None
    assert details_from_errors([{"loc": [], "type": ""}]) is None
