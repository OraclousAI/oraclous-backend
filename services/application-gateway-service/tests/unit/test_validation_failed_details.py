"""ORA-54 — ``details[]`` exists iff ``code == VALIDATION_FAILED``, and never reflects raw values.

§3 sub-rules covered:

* ``details[]`` is **present** only for VALIDATION_FAILED.
* Each ``details[]`` item is ``{field, issue}`` — exactly those two keys.
* ``issue`` is a machine token from a closed sub-vocabulary
  (``^[A-Z][A-Z0-9_]*$``), never the offending raw value (no reflected-PII /
  reflected-XSS surface).

The JSON Schema enforces these structurally; this file proves the impl
actually emits them that way through the live error pipeline (the schema
catches *malformed* shapes; the suite must catch *missing* enforcement, e.g.
the impl forgetting to add ``details`` at all, or accidentally including it
on a non-VALIDATION_FAILED response).

RED until the impl's translator emits ``details`` only for VALIDATION_FAILED
and only as ``{field, issue}`` with an uppercase machine-token ``issue``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.conftest import CANONICAL_EXCEPTION_NAMES, EXPECTED_CODES

pytestmark = pytest.mark.unit

_ISSUE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def test_validation_failed_response_includes_details(client: Any) -> None:
    response = client.post("/__probe__/raise-validation")
    body = response.json()
    assert "details" in body["error"], body


def test_validation_failed_details_have_field_and_issue_only(client: Any) -> None:
    body = client.post("/__probe__/raise-validation").json()
    for entry in body["error"]["details"]:
        assert set(entry.keys()) == {"field", "issue"}, entry


def test_validation_failed_issue_is_uppercase_machine_token(client: Any) -> None:
    body = client.post("/__probe__/raise-validation").json()
    for entry in body["error"]["details"]:
        assert _ISSUE_TOKEN.match(entry["issue"]), entry


@pytest.mark.parametrize(
    "code",
    [c for c in EXPECTED_CODES if c != "VALIDATION_FAILED" and c != "UNAUTHORIZED"],
)
def test_non_validation_failed_responses_omit_details(code: str, client: Any) -> None:
    cls_name = CANONICAL_EXCEPTION_NAMES[code]
    body = client.post(f"/__probe__/raise/{cls_name}").json()
    assert "details" not in body["error"], (code, body)
