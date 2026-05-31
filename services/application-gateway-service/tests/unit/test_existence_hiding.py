"""ORA-54 — the existence-hiding rule: prefer 404 over 403 when read is denied.

§3 verbatim:

    Existence-hiding rule: prefer 404 over 403 when the caller has no read
    permission, so the error does not confirm existence; use 403 only when
    the caller can already enumerate the resource.

The threat is information disclosure via the 403-vs-404 differential — a 403
on a resource the caller cannot read confirms the resource exists, even
though the caller couldn't otherwise enumerate it. The fix is to surface
NOT_FOUND (HTTP 404, code NOT_FOUND) instead, indistinguishable from a real
missing-resource case.

This file covers the rule end-to-end at the HTTP layer. The pure-translator
version of the same proof lives in ``test_envelope_translate.py``
(``test_unauthorized_default_read_permitted_false_translates_to_not_found``);
the two together ensure neither the translator nor the wiring can be the
weak link.

RED until the gateway error middleware downgrades
``Unauthorized(read_permitted=False)`` to a NOT_FOUND envelope (HTTP 404).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def test_unauthorized_without_read_permission_returns_404_not_found(
    client: Any, validator: Any
) -> None:
    response = client.post("/__probe__/raise-unauthorized?read_permitted=false")
    assert response.status_code == 404
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "NOT_FOUND"


def test_unauthorized_with_read_permission_returns_403_unauthorized(
    client: Any, validator: Any
) -> None:
    response = client.post("/__probe__/raise-unauthorized?read_permitted=true")
    assert response.status_code == 403
    body = response.json()
    errors = [e.message for e in validator.iter_errors(body)]
    assert not errors, errors
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_not_found_and_existence_hidden_responses_are_indistinguishable(
    client: Any,
) -> None:
    """A genuinely missing resource and an existence-hidden one must look identical.

    If the impl differentiates them — by status code, by ``code``, by
    ``message`` text, by ``retryable``, by ``details`` — the existence-hiding
    rule is defeated. The only field that may differ is ``requestId``
    (necessarily unique per request).
    """
    real_not_found = client.post("/__probe__/raise/NotFound")
    existence_hidden = client.post("/__probe__/raise-unauthorized?read_permitted=false")

    assert real_not_found.status_code == existence_hidden.status_code == 404

    real_body = real_not_found.json()["error"]
    hidden_body = existence_hidden.json()["error"]

    for field in ("code", "message", "retryable"):
        assert real_body[field] == hidden_body[field], (field, real_body, hidden_body)

    assert ("details" in real_body) is ("details" in hidden_body)
