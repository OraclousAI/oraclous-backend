"""ORA-54 — every probe route's response carries the §3 envelope (HTTP wiring).

The pure ``translate`` seam is covered in ``test_envelope_translate.py``. This
file covers the wiring: every canonical exception, when raised inside a real
request handler, surfaces through the gateway's error middleware as a
conforming envelope with the correct HTTP status from the §3 taxonomy.

The probe surface (mounted only when ``create_app(test_mode=True)``) is
documented in ``tests/conftest.py``. Briefly:

* ``POST /__probe__/raise/{ExceptionClassName}`` — default-construct + raise
* ``POST /__probe__/raise-validation`` — raise ValidationFailed with known details
* ``POST /__probe__/raise-unauthorized?read_permitted=…`` — raise Unauthorized

RED until ``create_app`` accepts ``test_mode=True`` and mounts the probes, and
the error middleware turns each raise into the canonical envelope.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import CANONICAL_EXCEPTION_NAMES, EXPECTED_CODES

pytestmark = pytest.mark.unit

# Codes whose probe path is bespoke (special probe route in conftest docstring).
_BESPOKE = {"VALIDATION_FAILED", "UNAUTHORIZED"}


@pytest.mark.parametrize("code", [c for c in EXPECTED_CODES if c not in _BESPOKE])
def test_probe_raise_default_constructed_returns_envelope(
    code: str, client: Any, validator: Any, taxonomy: dict[str, dict[str, Any]]
) -> None:
    """Raising the canonical exception for ``code`` produces a §3 envelope."""
    cls_name = CANONICAL_EXCEPTION_NAMES[code]
    response = client.post(f"/__probe__/raise/{cls_name}")
    expected_status = taxonomy[code]["http"]
    assert response.status_code == expected_status, response.text
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == code, body


def test_probe_raise_validation_returns_validation_failed_envelope(
    client: Any, validator: Any
) -> None:
    response = client.post("/__probe__/raise-validation")
    assert response.status_code == 400, response.text
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"] == [
        {"field": "email", "issue": "INVALID_FORMAT"},
        {"field": "age", "issue": "OUT_OF_RANGE"},
    ]


def test_probe_raise_unauthorized_default_returns_not_found(client: Any, validator: Any) -> None:
    """The conftest probe's default is ``read_permitted=false`` — existence-hide to 404."""
    response = client.post("/__probe__/raise-unauthorized")
    assert response.status_code == 404, response.text
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "NOT_FOUND"


def test_probe_raise_unauthorized_read_permitted_true_returns_403(
    client: Any, validator: Any
) -> None:
    response = client.post("/__probe__/raise-unauthorized?read_permitted=true")
    assert response.status_code == 403, response.text
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_probe_raise_unhandled_returns_internal_error(client: Any, validator: Any) -> None:
    """A bare ``raise Exception(...)`` is translated to a clean INTERNAL_ERROR envelope.

    The probe deliberately raises with a leaky message; the leak content itself
    is tested in ``test_sensitive_data_never_leaks.py``. Here we only assert
    that the response is the correct envelope at the correct status.
    """
    response = client.post("/__probe__/raise-unhandled")
    assert response.status_code == 500, response.text
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "INTERNAL_ERROR"


# --- retryable per §3 taxonomy --------------------------------------------


@pytest.mark.parametrize("code", [c for c in EXPECTED_CODES if c not in _BESPOKE])
def test_retryable_matches_taxonomy_default(
    code: str, client: Any, taxonomy: dict[str, dict[str, Any]]
) -> None:
    """Each probe response's ``retryable`` matches the §3 taxonomy's default.

    INTERNAL_ERROR is documented as ``retryable_overridable=true`` (server MAY
    set true when transient). The probe path here is a generic raise — the
    impl has no transient signal — so the default ``false`` must hold.
    """
    cls_name = CANONICAL_EXCEPTION_NAMES[code]
    response = client.post(f"/__probe__/raise/{cls_name}")
    body = response.json()
    assert body["error"]["retryable"] is taxonomy[code]["retryable_default"], body
