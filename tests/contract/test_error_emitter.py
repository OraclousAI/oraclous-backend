"""Conformance tests for the ``oraclous_errors`` emitter against the ORA-37 contract.

Pairs with ``test_error_envelope_fixture.py`` (which proves the fixture artifacts
are internally consistent); this proves the Python emitter never drifts from those
artifacts and always produces schema-valid, leak-free envelopes.

Marked ``unit`` (fast, isolated) and ``security`` (the §3 dominant risk is
sensitive-data leakage in an error body).
"""

from __future__ import annotations

import json
import re

import pytest
from jsonschema import Draft202012Validator
from oraclous_errors import (
    CODE_POLICY,
    REQUEST_ID_PATTERN,
    ErrorCode,
    FieldError,
    build_envelope,
    default_message,
    new_request_id,
    status_to_code,
)
from tools.contract.error_envelope import (
    load_forbidden_patterns,
    load_samples,
    load_schema,
    load_taxonomy,
    scan_forbidden,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema())


def _envelope_for(code: ErrorCode) -> dict:
    if code is ErrorCode.VALIDATION_FAILED:
        return build_envelope(
            code, request_id=new_request_id(), details=[FieldError("email", "INVALID_FORMAT")]
        )
    return build_envelope(code, request_id=new_request_id())


# --- the Python emitter mirrors the contract -------------------------------


def test_errorcode_matches_schema_enum() -> None:
    enum = set(load_schema()["properties"]["error"]["properties"]["code"]["enum"])
    assert {c.value for c in ErrorCode} == enum


def test_policy_matches_taxonomy() -> None:
    taxonomy = {row["code"]: row for row in load_taxonomy()["codes"]}
    assert set(taxonomy) == {c.value for c in ErrorCode}
    for code, policy in CODE_POLICY.items():
        row = taxonomy[code.value]
        assert policy.http_status == row["http"], code
        assert policy.retryable_default == row["retryable_default"], code


def test_default_message_matches_samples() -> None:
    samples = load_samples()
    for code in ErrorCode:
        assert default_message(code) == samples[code.value]["error"]["message"], code


# --- every built envelope is schema-valid and leak-free --------------------


@pytest.mark.parametrize("code", list(ErrorCode))
def test_build_envelope_is_schema_valid(code: ErrorCode, validator: Draft202012Validator) -> None:
    errors = [e.message for e in validator.iter_errors(_envelope_for(code))]
    assert not errors, errors


def test_built_envelopes_have_no_forbidden_substrings() -> None:
    patterns = load_forbidden_patterns()
    for code in ErrorCode:
        hits = scan_forbidden(json.dumps(_envelope_for(code)), patterns)
        assert not hits, f"{code.value}: {hits}"


def test_retryable_override_is_honoured(validator: Draft202012Validator) -> None:
    env = build_envelope(ErrorCode.INTERNAL_ERROR, request_id=new_request_id(), retryable=True)
    assert env["error"]["retryable"] is True
    assert validator.is_valid(env)


# --- requestId minting -----------------------------------------------------


def test_new_request_id_matches_contract_pattern() -> None:
    schema_pat = load_schema()["properties"]["error"]["properties"]["requestId"]["pattern"]
    for _ in range(100):
        rid = new_request_id()
        assert REQUEST_ID_PATTERN.match(rid)
        assert re.match(schema_pat, rid)


def test_request_ids_are_unique() -> None:
    assert len({new_request_id() for _ in range(1000)}) == 1000


# --- details discipline ----------------------------------------------------


def test_validation_failed_requires_details() -> None:
    with pytest.raises(ValueError):
        build_envelope(ErrorCode.VALIDATION_FAILED, request_id=new_request_id())


def test_details_forbidden_on_other_codes() -> None:
    with pytest.raises(ValueError):
        build_envelope(
            ErrorCode.NOT_FOUND, request_id=new_request_id(), details=[FieldError("x", "BAD")]
        )


# --- upstream status normalisation -----------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, ErrorCode.MALFORMED_REQUEST),
        (401, ErrorCode.UNAUTHENTICATED),
        (403, ErrorCode.UNAUTHORIZED),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (409, ErrorCode.CONFLICT),
        (413, ErrorCode.PAYLOAD_TOO_LARGE),
        (415, ErrorCode.UNSUPPORTED_MEDIA_TYPE),
        (429, ErrorCode.RATE_LIMITED),
        (418, ErrorCode.MALFORMED_REQUEST),  # unmapped 4xx
        (500, ErrorCode.INTERNAL_ERROR),
        (502, ErrorCode.SERVICE_UNAVAILABLE),
        (503, ErrorCode.SERVICE_UNAVAILABLE),
        (504, ErrorCode.GATEWAY_TIMEOUT),
        (507, ErrorCode.SERVICE_UNAVAILABLE),  # unmapped 5xx
    ],
)
def test_status_to_code(status: int, expected: ErrorCode) -> None:
    assert status_to_code(status) == expected


def test_status_to_code_never_synthesises_validation_failed() -> None:
    for status in range(400, 600):
        assert status_to_code(status) is not ErrorCode.VALIDATION_FAILED
