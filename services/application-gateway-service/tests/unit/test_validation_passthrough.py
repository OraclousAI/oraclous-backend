"""Unit: leak-safe 422 + 409-needs_credential extraction — adversarial inputs (#225, #281, #502)."""

from __future__ import annotations

import json

from oraclous_application_gateway_service.domain.validation_passthrough import (
    details_from_errors,
    extract_needs_credential,
    extract_validation_details,
)
from oraclous_errors import ErrorCode, NeedsCredential, build_envelope, new_request_id
from tools.contract.error_envelope import scan_forbidden


def _raw(detail: object) -> bytes:
    import json

    return json.dumps({"detail": detail}).encode()


def _nc_raw(needs_credential: object) -> bytes:
    # the registry 409 body shape (#490): detail/error_code + the nested needs_credential token
    return json.dumps(
        {
            "detail": "credential not mapped",
            "error_code": "credential_not_mapped",
            "needs_credential": needs_credential,
            "login_url": "https://idp.internal:8443/oauth/authorize?secret=tvly-xyz",
            "missing_scopes": ["read"],
        }
    ).encode()


def test_needs_credential_clean_token_extracts() -> None:
    nc = extract_needs_credential(_nc_raw({"requirement_id": "api_key", "provider": "web_search"}))
    assert nc == NeedsCredential("api_key", "web_search")


def test_needs_credential_never_surfaces_login_url_or_scopes() -> None:
    # the registry body carries a login_url with an internal host + a secret query param — the
    # extractor must surface ONLY requirement_id/provider, never the URL/host/secret/scopes.
    nc = extract_needs_credential(_nc_raw({"requirement_id": "api_key", "provider": "web_search"}))
    assert nc is not None
    blob = f"{nc.requirement_id} {nc.provider}"
    assert "idp.internal" not in blob and "tvly-xyz" not in blob and "read" not in blob
    assert scan_forbidden(blob) == []


def test_needs_credential_url_or_host_provider_is_neutralised() -> None:
    # an attacker-shaped provider can never carry a URL/host/@ through the token charset
    for bad in ("http://evil/cb", "idp.internal:8443", "user@host", "a/../b"):
        nc = extract_needs_credential(_nc_raw({"requirement_id": "api_key", "provider": bad}))
        if nc is not None:
            assert "/" not in nc.provider and ":" not in nc.provider and "@" not in nc.provider
            # and whatever survives is always a buildable (contract-conformant) envelope
            build_envelope(
                ErrorCode.CREDENTIALS_REQUIRED, request_id=new_request_id(), needs_credential=nc
            )


def test_needs_credential_oversized_is_capped() -> None:
    nc = extract_needs_credential(_nc_raw({"requirement_id": "a" * 500, "provider": "b" * 500}))
    assert nc is not None and len(nc.requirement_id) <= 64 and len(nc.provider) <= 48


def test_needs_credential_absent_or_malformed_returns_none() -> None:
    assert extract_needs_credential(b'{"detail":"x","error_code":"no_executor"}') is None
    assert extract_needs_credential(_nc_raw("not-a-dict")) is None
    assert extract_needs_credential(_nc_raw({"requirement_id": "api_key"})) is None  # no provider
    assert extract_needs_credential(b"not json") is None
    assert extract_needs_credential(b"") is None


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


def test_krs_typed_eval_422s_survive_the_extractor() -> None:
    # #333: KRS emits its typed eval errors in the Pydantic LIST shape precisely so this
    # extractor relays them (loc + type → field + token); pin both bodies it sends.
    out = extract_validation_details(
        _raw([{"loc": ["eval"], "type": "eval_judge_not_configured", "msg": "set the key"}])
    )
    assert out is not None
    assert out[0].field == "eval" and out[0].issue == "EVAL_JUDGE_NOT_CONFIGURED"
    out = extract_validation_details(
        _raw([{"loc": ["body", "metrics"], "type": "no_valid_metrics", "msg": "nothing left"}])
    )
    assert out is not None
    assert out[0].field == "metrics" and out[0].issue == "NO_VALID_METRICS"


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
