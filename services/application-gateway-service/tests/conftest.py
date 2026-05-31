"""Shared fixtures for the gateway error-envelope test suite (ORA-54).

This conftest is intentionally minimal — every fixture either loads from the
shared error-envelope contract artifacts under ``packages/errors/contract/``
(produced by ORA-53 / devops-implementer) or constructs the gateway test app.

The gateway test app is produced by
``oraclous_application_gateway_service.create_app(test_mode=True)``, which the
implementer is expected to publish. When ``test_mode=True`` the app mounts a
small probe surface that lets the suite trigger each envelope ``code``
deterministically without coupling to any business endpoint:

* ``POST /__probe__/raise/{ExceptionClassName}`` — instantiate the named class
  from ``oraclous_application_gateway_service.errors`` (default-constructed) and
  raise it inside the request handler. Used to drive every code that is reached
  by raising a canonical exception.
* ``POST /__probe__/raise-validation`` — raise
  ``ValidationFailed(details=[("email", "INVALID_FORMAT"), ("age", "OUT_OF_RANGE")])``
  so the suite can assert ``details[]`` shape without inventing payloads.
* ``POST /__probe__/raise-unauthorized`` — read query ``read_permitted`` (default
  ``"false"``) and raise ``Unauthorized(read_permitted=bool)`` so the suite can
  assert the existence-hiding rule (§3: prefer 404 over 403 when the caller has
  no read permission).
* ``POST /__probe__/raise-unhandled`` — raise a bare ``Exception`` whose message
  intentionally contains several forbidden substrings (stack-trace markers, a
  ``password=`` assignment, a ``Bearer …`` token, an internal hostname, a SQL
  fragment). The suite asserts the gateway translates this to the canonical
  ``INTERNAL_ERROR`` envelope without any of those substrings reaching the body.
* ``POST /__probe__/echo`` — accepts JSON body ``{"value": str}`` and returns
  200 with ``{"value": value}``. Used to drive framework-native error triggers:
  malformed JSON, wrong content-type, schema-mismatched body, etc.

Framework choice: FastAPI is provisionally pinned in
``services/application-gateway-service/pyproject.toml`` to match the
``auth-service`` precedent (the only other public-edge service so far). The
suite uses ``fastapi.testclient.TestClient`` purely for ergonomics — every
behavioural assertion is on the response **body and status only**, never on
framework internals, so the impl is free to swap frameworks at Tests Review
and the tests adapt with a one-line change to the client fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import re

    from fastapi.testclient import TestClient
    from jsonschema import Draft202012Validator

# The 13-entry closed taxonomy from Interface Contracts §3. Mirrored in
# ``packages/errors/contract/error-code-taxonomy.json``; this constant lets each
# test address codes by symbol without re-parsing the file every call.
EXPECTED_CODES: tuple[str, ...] = (
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
)

# Map from code -> the canonical exception class name the impl publishes under
# ``oraclous_application_gateway_service.errors``. The names are PascalCase
# transforms of the code; pinning the mapping here means a name drift in impl
# fails one test loudly rather than the whole suite obscurely.
CANONICAL_EXCEPTION_NAMES: dict[str, str] = {
    "VALIDATION_FAILED": "ValidationFailed",
    "MALFORMED_REQUEST": "MalformedRequest",
    "UNAUTHENTICATED": "Unauthenticated",
    "UNAUTHORIZED": "Unauthorized",
    "NOT_FOUND": "NotFound",
    "METHOD_NOT_ALLOWED": "MethodNotAllowed",
    "CONFLICT": "Conflict",
    "PAYLOAD_TOO_LARGE": "PayloadTooLarge",
    "UNSUPPORTED_MEDIA_TYPE": "UnsupportedMediaType",
    "RATE_LIMITED": "RateLimited",
    "INTERNAL_ERROR": "InternalError",
    "SERVICE_UNAVAILABLE": "ServiceUnavailable",
    "GATEWAY_TIMEOUT": "GatewayTimeout",
}

# req_-prefixed opaque correlation handle (Interface Contracts §3). The schema
# enforces this; the constant lets request_id-only tests assert without
# re-parsing the schema each call.
REQUEST_ID_PATTERN = r"^req_[0-9A-Za-z]+$"


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    """The shared error-envelope JSON Schema from the ORA-53 fixture."""
    from tools.contract.error_envelope import load_schema

    return load_schema()


@pytest.fixture(scope="session")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    """A Draft 2020-12 validator over the shared schema, ready to call."""
    from jsonschema import Draft202012Validator

    return Draft202012Validator(schema)


@pytest.fixture(scope="session")
def taxonomy() -> dict[str, dict[str, Any]]:
    """Map of code -> taxonomy row (http, retryable_default, …) from the §3 table."""
    from tools.contract.error_envelope import load_taxonomy

    return {row["code"]: row for row in load_taxonomy()["codes"]}


@pytest.fixture(scope="session")
def forbidden_patterns() -> list[Any]:
    """The ORA-53 forbidden-substring pattern catalogue (sensitive-data rules §3)."""
    from tools.contract.error_envelope import load_forbidden_patterns

    return load_forbidden_patterns()


@pytest.fixture(scope="session")
def request_id_re() -> re.Pattern[str]:
    import re

    return re.compile(REQUEST_ID_PATTERN)


@pytest.fixture()
def client() -> TestClient:
    """A FastAPI ``TestClient`` over ``create_app(test_mode=True)``.

    Function-scoped so any per-test app mutation (rare; reserved for the
    framework-native trigger tests) does not bleed across tests.
    """
    from fastapi.testclient import TestClient
    from oraclous_application_gateway_service import create_app

    return TestClient(create_app(test_mode=True), raise_server_exceptions=False)
