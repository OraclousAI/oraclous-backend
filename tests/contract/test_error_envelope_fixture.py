"""Reference self-test for the gateway error-envelope contract fixture.

Contract ORA-37 / Interface Contracts §3. This proves the shared artifacts under
``packages/errors/contract/`` are internally consistent, that the JSON Schema
enforces the §3 constraints, and runs the forbidden-substring negative test over
the curated samples. The backend api error-path tests (ORA-54) and the frontend
api-client tests (ORA-55) consume the *same* artifacts; this is the fixture's own
enforcement that it is correct before either side depends on it.

Marked ``unit`` (fast, isolated) and ``security`` (the §3 dominant risk is
sensitive-data leakage), so it runs in both the CI quality and security jobs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from tools.contract.error_envelope import (
    load_forbidden_patterns,
    load_samples,
    load_schema,
    load_taxonomy,
    scan_forbidden,
    verify_checksums,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

EXPECTED_CODES = {
    "VALIDATION_FAILED",
    "MALFORMED_REQUEST",
    "UNAUTHENTICATED",
    "UNAUTHORIZED",
    "NOT_FOUND",
    "METHOD_NOT_ALLOWED",
    "CONFLICT",
    "PAYLOAD_TOO_LARGE",
    "UNSUPPORTED_MEDIA_TYPE",
    "RATE_LIMITED",
    "INTERNAL_ERROR",
    "SERVICE_UNAVAILABLE",
    "GATEWAY_TIMEOUT",
}

# A minimal valid (non-VALIDATION_FAILED) envelope inner object used as a base for
# negative-case mutation.
BASE: dict[str, Any] = {
    "code": "INTERNAL_ERROR",
    "message": "An unexpected error occurred.",
    "requestId": "req_01TESTTESTTESTTESTTEST00",
    "retryable": False,
}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return load_schema()


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


def _enum(schema: dict[str, Any]) -> set[str]:
    return set(schema["properties"]["error"]["properties"]["code"]["enum"])


def _is_valid(validator: Draft202012Validator, error_obj: dict[str, Any]) -> bool:
    return validator.is_valid({"error": error_obj})


# --- schema sanity ---------------------------------------------------------


def test_schema_is_itself_valid(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)  # raises if the schema is malformed


def test_code_enum_is_the_closed_set_of_13(schema: dict[str, Any]) -> None:
    enum = schema["properties"]["error"]["properties"]["code"]["enum"]
    assert set(enum) == EXPECTED_CODES
    assert len(enum) == 13


def test_additional_properties_false_at_every_level(schema: dict[str, Any]) -> None:
    err = schema["properties"]["error"]
    assert schema["additionalProperties"] is False
    assert err["additionalProperties"] is False
    assert err["properties"]["details"]["items"]["additionalProperties"] is False


# --- samples ---------------------------------------------------------------


def test_one_valid_sample_per_code() -> None:
    assert set(load_samples()) == EXPECTED_CODES


@pytest.mark.parametrize("code", sorted(EXPECTED_CODES))
def test_sample_validates_and_matches_filename(code: str, validator: Draft202012Validator) -> None:
    sample = load_samples()[code]
    errors = [e.message for e in validator.iter_errors(sample)]
    assert not errors, errors
    assert sample["error"]["code"] == code


def test_taxonomy_covers_exactly_the_enum(schema: dict[str, Any]) -> None:
    codes = {row["code"] for row in load_taxonomy()["codes"]}
    assert codes == _enum(schema)


def test_sample_retryable_matches_taxonomy_default() -> None:
    taxonomy = {row["code"]: row for row in load_taxonomy()["codes"]}
    for code, sample in load_samples().items():
        assert sample["error"]["retryable"] == taxonomy[code]["retryable_default"]


# --- negative schema cases -------------------------------------------------


def test_rejects_unknown_field_like_stack(validator: Draft202012Validator) -> None:
    assert not _is_valid(validator, {**BASE, "stack": "Traceback ..."})


def test_rejects_top_level_extra_field(validator: Draft202012Validator) -> None:
    assert not validator.is_valid({"error": dict(BASE), "stack": "x"})


def test_rejects_missing_request_id(validator: Draft202012Validator) -> None:
    assert not _is_valid(validator, {k: v for k, v in BASE.items() if k != "requestId"})


def test_rejects_missing_retryable(validator: Draft202012Validator) -> None:
    assert not _is_valid(validator, {k: v for k, v in BASE.items() if k != "retryable"})


def test_rejects_code_outside_enum(validator: Draft202012Validator) -> None:
    assert not _is_valid(validator, {**BASE, "code": "TEAPOT"})


def test_rejects_freeform_request_id(validator: Draft202012Validator) -> None:
    assert not _is_valid(validator, {**BASE, "requestId": "internal-host-42"})


def test_details_only_on_validation_failed(validator: Draft202012Validator) -> None:
    # details on a non-VALIDATION_FAILED code is rejected
    assert not _is_valid(validator, {**BASE, "details": [{"field": "x", "issue": "BAD"}]})
    # VALIDATION_FAILED requires details
    vf = {
        "code": "VALIDATION_FAILED",
        "message": "One or more fields are invalid.",
        "requestId": "req_01TESTTESTTESTTESTTEST00",
        "retryable": False,
    }
    assert not _is_valid(validator, vf)
    # VALIDATION_FAILED with well-formed details is accepted
    assert _is_valid(validator, {**vf, "details": [{"field": "email", "issue": "INVALID_FORMAT"}]})


def test_rejects_details_with_extra_key(validator: Draft202012Validator) -> None:
    obj = {
        "code": "VALIDATION_FAILED",
        "message": "One or more fields are invalid.",
        "requestId": "req_01TESTTESTTESTTESTTEST00",
        "retryable": False,
        "details": [{"field": "email", "issue": "INVALID_FORMAT", "value": "a@b.com"}],
    }
    assert not _is_valid(validator, obj)


def test_rejects_reflected_value_in_issue(validator: Draft202012Validator) -> None:
    # issue must be an uppercase machine token, never a reflected raw value
    obj = {
        "code": "VALIDATION_FAILED",
        "message": "One or more fields are invalid.",
        "requestId": "req_01TESTTESTTESTTESTTEST00",
        "retryable": False,
        "details": [{"field": "email", "issue": "not-an-email@x"}],
    }
    assert not _is_valid(validator, obj)


# --- forbidden-substring negative test -------------------------------------


def test_curated_samples_contain_no_forbidden_substrings() -> None:
    patterns = load_forbidden_patterns()
    for code, sample in load_samples().items():
        hits = scan_forbidden(json.dumps(sample), patterns)
        assert not hits, f"{code} sample tripped forbidden patterns: {hits}"


def test_scanner_matches_each_pattern_example() -> None:
    for pattern in load_forbidden_patterns():
        assert pattern.regex.search(pattern.example), (
            f"pattern {pattern.id} did not match its own example"
        )


def test_every_pattern_maps_to_a_sensitive_data_rule() -> None:
    for pattern in load_forbidden_patterns():
        assert 1 <= pattern.rule <= 8


def test_scanner_flags_a_leaky_body() -> None:
    leaky = json.dumps(
        {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "NullPointerException at com.x.Y(Y.java:10) on db-1.svc.cluster.local",
                "requestId": "req_01TESTTESTTESTTESTTEST00",
                "retryable": False,
            }
        }
    )
    hits = set(scan_forbidden(leaky))
    assert {
        "exception_class_name",
        "stack_frame_with_location",
        "internal_dns_suffix",
    } <= hits


# --- checksum integrity ----------------------------------------------------


def test_checksum_manifest_matches_artifacts() -> None:
    errors = verify_checksums()
    assert not errors, errors
