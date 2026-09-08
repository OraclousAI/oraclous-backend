"""#949 ruling Q4 (C5) — `MODEL_CREDENTIAL_REQUIRED` is recorded in the cross-repo error-code
contract (`packages/errors/contract/`), not just in the Python emitter. `test_error_emitter.py`
already proves the emitter never DRIFTS from the contract for whatever codes exist; this pins that
the new code actually EXISTS in both — schema enum, taxonomy row, and a curated sample — mirroring
how `MODEL_NOT_CONNECTED`/`IDEA_TOO_VAGUE` were added for #866.

`oraclous_errors` and `tools.contract.error_envelope` already exist; nothing here is a not-yet-built
seam, so every import is module-level per house style. The failures below are honest assertion
failures (RED), not import errors.
"""

from __future__ import annotations

import pytest
from oraclous_errors import ErrorCode, default_message
from tools.contract.error_envelope import load_samples, load_schema, load_taxonomy

pytestmark = pytest.mark.unit

_CODE = "MODEL_CREDENTIAL_REQUIRED"


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


def test_the_python_emitter_and_the_contract_agree_on_the_new_code() -> None:
    """Mirrors `test_error_emitter.py::test_default_message_matches_samples`, scoped to just the
    new member so this test is meaningful even before every OTHER code is re-checked."""
    code = getattr(ErrorCode, _CODE, None)
    assert code is not None, f"oraclous_errors.ErrorCode has no {_CODE} member yet"
    samples = load_samples()
    assert default_message(code) == samples[_CODE]["error"]["message"]
