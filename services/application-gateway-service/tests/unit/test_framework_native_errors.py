"""ORA-54 — framework-native error triggers also produce the canonical envelope.

The §3 acceptance is "every 4xx/5xx response", not "every response from our
handler". Framework defaults (Starlette/FastAPI's built-in 404 for unknown
routes, 405 for wrong method, 422 for body-validation, etc.) emit their own
shapes by default — ``{"detail": "Not Found"}`` and friends — which violate
the contract: they have no ``code``, no ``requestId``, no ``retryable``, and
the top-level key is wrong.

This file proves that every framework-default error path is intercepted and
re-emitted as a §3 envelope. The probes in ``conftest.py`` give us:

* unknown route → framework 404 → must surface as NOT_FOUND envelope
* wrong method on a known route → framework 405 → METHOD_NOT_ALLOWED envelope
* malformed JSON body on ``/__probe__/echo`` → MALFORMED_REQUEST envelope
* body schema mismatch on ``/__probe__/echo`` → VALIDATION_FAILED envelope
* unsupported content-type on ``/__probe__/echo`` → UNSUPPORTED_MEDIA_TYPE

RED until the gateway installs handlers for the relevant framework exceptions
(``HTTPException`` 404/405, ``RequestValidationError``, JSON-decode errors,
content-type rejection) and routes each through the canonical translator.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def test_unknown_route_returns_not_found_envelope(client: Any, validator: Any) -> None:
    response = client.get("/this-route-does-not-exist-anywhere-12345")
    assert response.status_code == 404
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "NOT_FOUND"


def test_wrong_method_returns_method_not_allowed_envelope(client: Any, validator: Any) -> None:
    # The probe routes are POST; a GET must surface METHOD_NOT_ALLOWED.
    response = client.get("/__probe__/raise/InternalError")
    assert response.status_code == 405
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_malformed_json_body_returns_malformed_request_envelope(
    client: Any, validator: Any
) -> None:
    response = client.post(
        "/__probe__/echo",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "MALFORMED_REQUEST"


def test_schema_mismatched_body_returns_validation_failed_envelope(
    client: Any, validator: Any
) -> None:
    """A well-formed JSON body that violates the route's schema → VALIDATION_FAILED.

    The echo probe expects ``{"value": str}``. Sending the wrong shape exercises
    the framework's body-validation path (FastAPI's ``RequestValidationError``),
    which must surface as VALIDATION_FAILED with ``details[]`` populated.
    """
    response = client.post(
        "/__probe__/echo",
        json={"wrong_key": 1},
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"], body


def test_unsupported_media_type_returns_unsupported_media_type_envelope(
    client: Any, validator: Any
) -> None:
    response = client.post(
        "/__probe__/echo",
        content=b"<value>x</value>",
        headers={"Content-Type": "application/xml"},
    )
    # FastAPI/Starlette by default returns 422 for non-JSON bodies on a JSON
    # endpoint; the gateway must reclassify to 415 with UNSUPPORTED_MEDIA_TYPE
    # to honour the §3 taxonomy.
    assert response.status_code == 415
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
