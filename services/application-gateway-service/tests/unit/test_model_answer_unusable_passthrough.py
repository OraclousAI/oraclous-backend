"""#1151 — the reader/terminal-run sibling of `test_model_credential_rejected_passthrough.py`
(#1108): the harness/engine intake read-back also refuses when the model DID answer but the
answer could not be used — every ``_peel`` parse failure, and every non-SUCCEEDED terminal
(FAILED/REJECTED/COST_BUDGET) other than a credential rejection. That refusal crosses the
gateway's boundary the same way — an ALLOW-LISTED ``error_code``, read from the upstream body,
and nothing else.

`extract_error_code` already exists and already works generically; what is missing is
`MODEL_ANSWER_UNUSABLE` in the allow-list itself (`_RELAYABLE_CODES`). Without it, the engine's
typed 502 is dropped at the edge and the console gets the generic status-derived envelope.

Marked `unit` and `security`, mirroring `test_model_credential_rejected_passthrough.py` — the
risk here is a leak channel, not a feature.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

_CODE = "MODEL_ANSWER_UNUSABLE"


def _extract(raw: bytes):  # noqa: ANN202 — mirrors extract_error_code's own return type
    from oraclous_application_gateway_service.domain.validation_passthrough import (  # noqa: PLC0415, E501
        extract_error_code,
    )

    return extract_error_code(raw)


def _body(**kw: object) -> bytes:
    return json.dumps(kw).encode()


def test_model_answer_unusable_crosses_the_boundary() -> None:
    assert _extract(_body(error_code=_CODE)) == _CODE


def test_the_code_is_read_from_a_fastapi_detail_wrapper_too() -> None:
    """The read-back raises via `IntakeReadbackError(..., error_code=...)`, which the engine's
    exception handler serialises under FastAPI's nested-``detail`` shape, mirroring the other
    #866/#949/#1108 refusals."""
    raw = json.dumps({"detail": {"error_code": _CODE}}).encode()
    assert _extract(raw) == _CODE


def test_the_allow_list_still_rejects_everything_it_rejected_before() -> None:
    """Guards against a widened allow-list swallowing the existing boundary (e.g. matching on a
    prefix/substring instead of an exact new member, or case-folding)."""
    assert _extract(_body(error_code="UNAUTHORIZED")) is None
    assert _extract(_body(error_code="TOTALLY_MADE_UP")) is None
    assert _extract(_body(error_code="model_answer_unusable")) is None  # case-sensitive
    assert _extract(_body(error_code="MODEL_ANSWER_UNUSABLE_X")) is None  # near-miss
    assert _extract(_body(error_code="MODEL_ANSWER_UNUSABL")) is None  # near-miss


def test_nothing_but_the_code_is_carried() -> None:
    out = _extract(
        json.dumps(
            {
                "error_code": _CODE,
                "msg": "reader could not parse the model's output: ```not json at all```",
                "stack": "Traceback (most recent call last): ...",
            }
        ).encode()
    )
    assert out == _CODE
    assert isinstance(out, str)
