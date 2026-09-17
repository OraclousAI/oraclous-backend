"""#1108 — the harness/engine half of curated model-credential refusals pins
`MODEL_CREDENTIAL_REQUIRED` (#949, no credential designated); this pins its sibling,
`MODEL_CREDENTIAL_REJECTED` — the provider that IS configured refuses the key (401/403 from the
LLM client), surfaced through the intake read-back rather than left as a generic READBACK_FAILED.

Mirrors `test_model_credential_required_taxonomy.py`: schema enum, taxonomy row, curated sample,
and Python emitter must all agree the new code exists before the gateway/engine work relies on it.

`oraclous_errors` and `tools.contract.error_envelope` already exist; nothing here is a not-yet-built
seam, so every import is module-level per house style. The failures below are honest assertion
failures (RED), not import errors.
"""

from __future__ import annotations

import pytest
from oraclous_errors import CODE_POLICY, ErrorCode, default_message, http_status_for
from tools.contract.error_envelope import load_samples, load_schema, load_taxonomy

pytestmark = pytest.mark.unit

_CODE = "MODEL_CREDENTIAL_REJECTED"
_EXPECTED_MESSAGE = (
    "Your model provider refused the connected model key. Replace the key and try again."
)


def test_the_schema_enum_lists_the_new_code() -> None:
    enum = set(load_schema()["properties"]["error"]["properties"]["code"]["enum"])
    assert _CODE in enum


def test_the_taxonomy_json_carries_a_row_for_the_new_code() -> None:
    taxonomy = {row["code"]: row for row in load_taxonomy()["codes"]}
    assert _CODE in taxonomy
    row = taxonomy[_CODE]
    assert row["http"] == 422
    assert row["retryable_default"] is False


def test_a_curated_sample_exists_for_the_new_code() -> None:
    samples = load_samples()
    assert _CODE in samples
    assert samples[_CODE]["error"]["code"] == _CODE


def test_the_curated_message_is_pinned_exactly() -> None:
    """The message is gateway-authored (#1108 ruling 4): no key, no credential name — the client
    already knows which credential_id it sent. Pinned literally so the [impl] cannot drift to a
    plausible-sounding paraphrase."""
    samples = load_samples()
    assert _CODE in samples
    assert samples[_CODE]["error"]["message"] == _EXPECTED_MESSAGE


def test_the_python_emitter_and_the_contract_agree_on_the_new_code() -> None:
    """Mirrors `test_error_emitter.py::test_default_message_matches_samples`, scoped to just the
    new member so this test is meaningful even before every OTHER code is re-checked."""
    code = getattr(ErrorCode, _CODE, None)
    assert code is not None, f"oraclous_errors.ErrorCode has no {_CODE} member yet"
    samples = load_samples()
    assert default_message(code) == samples[_CODE]["error"]["message"]


def test_the_new_code_carries_a_422_curated_policy() -> None:
    """422, not 401/403 (would clash with UNAUTHENTICATED/UNAUTHORIZED and log the user out) —
    #1108 ruling 1."""
    code = getattr(ErrorCode, _CODE, None)
    assert code is not None, f"oraclous_errors.ErrorCode has no {_CODE} member yet"
    assert http_status_for(code) == 422
    assert CODE_POLICY[code].retryable_default is False
