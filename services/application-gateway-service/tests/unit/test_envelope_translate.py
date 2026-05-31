"""ORA-54 — every canonical exception translates to a §3-conforming envelope.

These tests pin the **pure mapping seam**:

    oraclous_application_gateway_service.errors.translate(
        exc: Exception, *, request_id: str
    ) -> dict

Rationale for a pure seam: HTTP-level tests exercise the wiring, but the
envelope contract is fundamentally a *response shape* contract. A pure
``Exception -> dict`` function lets the suite assert the shape for every code
in microseconds, without TestClient overhead, and gives the impl a single
place to maintain the mapping. The framework error handler then becomes a
two-line shim: ``request_id = …; return JSONResponse(translate(exc, …),
status_code=STATUS_BY_CODE[envelope['error']['code']])``.

What's pinned:

* One canonical exception class per envelope ``code`` lives under
  ``oraclous_application_gateway_service.errors``. Names: see
  ``CANONICAL_EXCEPTION_NAMES`` in ``conftest.py``.
* Every canonical exception, default-constructed, produces an envelope that
  validates against the shared schema with ``code`` == the matching constant.
* ``Unauthorized(read_permitted=False)`` (the default) translates to
  ``NOT_FOUND`` — the existence-hiding rule lives in the translator, not in
  every call-site (cf. ``test_existence_hiding.py`` for the HTTP-level proof).
* ``ValidationFailed(details=[...])`` translates with the ``details[]`` array
  flattened to the §3 ``[{field, issue}]`` shape (cf.
  ``test_validation_failed_details.py``).
* A bare ``Exception`` translates to ``INTERNAL_ERROR`` (fail-closed; an
  unknown exception class must never bypass the envelope).

RED until ``oraclous_application_gateway_service.errors.translate`` and the
canonical exception classes exist.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import CANONICAL_EXCEPTION_NAMES, EXPECTED_CODES

pytestmark = pytest.mark.unit

# Codes whose canonical exception cannot be default-constructed (they carry
# semantic args that change the envelope). They get their own bespoke test
# below; this list keeps the parametrised "default-construct" sweep honest.
_NON_DEFAULT_CONSTRUCT = {"VALIDATION_FAILED", "UNAUTHORIZED"}


def _import_errors_module():
    import oraclous_application_gateway_service.errors as errors_module

    return errors_module


def _exception_class(code: str) -> type[Exception]:
    errors = _import_errors_module()
    name = CANONICAL_EXCEPTION_NAMES[code]
    cls = getattr(errors, name, None)
    assert cls is not None, (
        f"oraclous_application_gateway_service.errors must publish {name!r} "
        f"for envelope code {code!r}"
    )
    return cls


def _translate(exc: Exception, *, request_id: str = "req_TEST00000000000") -> dict[str, Any]:
    errors = _import_errors_module()
    translate = getattr(errors, "translate", None)
    assert translate is not None, (
        "oraclous_application_gateway_service.errors must publish "
        "translate(exc, *, request_id) -> dict"
    )
    return translate(exc, request_id=request_id)


# --- per-code envelope conformance ----------------------------------------


@pytest.mark.parametrize(
    "code",
    [c for c in EXPECTED_CODES if c not in _NON_DEFAULT_CONSTRUCT],
)
def test_default_constructed_exception_translates_to_matching_code(
    code: str, validator: Any
) -> None:
    cls = _exception_class(code)
    body = _translate(cls())
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, f"{code} envelope failed schema: {errors}"
    assert body["error"]["code"] == code, body
    # message must be present, non-empty, and not the exception class name —
    # §3 requires a curated, generic human-readable string.
    assert isinstance(body["error"]["message"], str) and body["error"]["message"], body
    assert cls.__name__ not in body["error"]["message"], body


def test_validation_failed_translates_with_details(validator: Any) -> None:
    cls = _exception_class("VALIDATION_FAILED")
    exc = cls(details=[("email", "INVALID_FORMAT"), ("age", "OUT_OF_RANGE")])
    body = _translate(exc)
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"] == [
        {"field": "email", "issue": "INVALID_FORMAT"},
        {"field": "age", "issue": "OUT_OF_RANGE"},
    ]


def test_unauthorized_default_read_permitted_false_translates_to_not_found(
    validator: Any,
) -> None:
    """The §3 existence-hiding rule lives in the translator.

    Default-constructed ``Unauthorized`` carries ``read_permitted=False`` (safer
    default — assume the caller cannot enumerate the resource), so the canonical
    translation is ``NOT_FOUND`` and the body is indistinguishable from a
    genuine missing-resource case. ``Unauthorized(read_permitted=True)`` only
    surfaces ``UNAUTHORIZED`` when the caller already can enumerate.
    """
    cls = _exception_class("UNAUTHORIZED")
    body = _translate(cls())
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "NOT_FOUND", body


def test_unauthorized_read_permitted_true_translates_to_unauthorized(validator: Any) -> None:
    cls = _exception_class("UNAUTHORIZED")
    body = _translate(cls(read_permitted=True))
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "UNAUTHORIZED", body


# --- non-canonical exceptions fail-closed to INTERNAL_ERROR ----------------


def test_unknown_exception_translates_to_internal_error(validator: Any) -> None:
    """Fail-closed: any exception not in the canonical hierarchy → INTERNAL_ERROR.

    The threat is that an upstream library's exception leaks through with its
    own message and type. The translator must never trust an exception it does
    not recognise — it must wrap it in a generic INTERNAL_ERROR envelope.
    """
    body = _translate(RuntimeError("anything"))
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "INTERNAL_ERROR", body


def test_unknown_exception_message_is_not_reflected(validator: Any) -> None:
    """The exception's own message text MUST NOT appear in the envelope message."""
    secret_marker = "MARKER_SHOULD_NEVER_APPEAR_IN_BODY_8f3c2a"  # noqa: S105 — test sentinel, not a credential
    body = _translate(RuntimeError(secret_marker))
    assert secret_marker not in body["error"]["message"], body
    # belt + braces: serialise the whole envelope and grep
    import json

    assert secret_marker not in json.dumps(body), body


# --- request_id propagation -----------------------------------------------


@pytest.mark.parametrize(
    "code",
    [c for c in EXPECTED_CODES if c not in _NON_DEFAULT_CONSTRUCT],
)
def test_request_id_is_propagated_into_envelope(code: str) -> None:
    """``translate`` must place the caller-provided ``request_id`` on the envelope.

    The handler shim is the single source of request-id generation; the
    translator simply propagates. This decoupling lets the handler use the
    framework's request id without the translator knowing about it.
    """
    cls = _exception_class(code)
    body = _translate(cls(), request_id="req_FIXEDFORTEST00000000")
    assert body["error"]["requestId"] == "req_FIXEDFORTEST00000000"
