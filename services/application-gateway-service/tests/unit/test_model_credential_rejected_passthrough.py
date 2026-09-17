"""#1108 — the sibling of `test_model_credential_required_passthrough.py` (#949 Q4/C5): the
harness/engine intake read-back also refuses when the org's designated model credential IS
present but the provider itself rejects the key (401/403 from the LLM client). That refusal
crosses the gateway's 4xx boundary the same way — an ALLOW-LISTED `error_code`, read from the
upstream body, and nothing else.

`extract_error_code` already exists and already works generically; what is missing is
`MODEL_CREDENTIAL_REJECTED` in the allow-list itself (`_RELAYABLE_CODES`). Without it, the
engine's typed 422 is dropped at the edge and the console gets the generic status-derived
envelope.

Marked `unit` and `security`, mirroring `test_model_credential_required_passthrough.py` — the
risk here is a leak channel, not a feature.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _extract(raw: bytes):  # noqa: ANN202 — mirrors extract_error_code's own return type
    from oraclous_application_gateway_service.domain.validation_passthrough import (  # noqa: PLC0415, E501
        extract_error_code,
    )

    return extract_error_code(raw)


def _body(**kw: object) -> bytes:
    return json.dumps(kw).encode()


def test_model_credential_rejected_crosses_the_boundary() -> None:
    assert _extract(_body(error_code="MODEL_CREDENTIAL_REJECTED")) == "MODEL_CREDENTIAL_REJECTED"


def test_the_code_is_read_from_a_fastapi_detail_wrapper_too() -> None:
    """The read-back raises via `HTTPException(status_code=422, detail={"error_code": ...})` —
    the nested-under-`detail` shape, mirroring how the engine's other #866/#949 refusals arrive."""
    raw = json.dumps({"detail": {"error_code": "MODEL_CREDENTIAL_REJECTED"}}).encode()
    assert _extract(raw) == "MODEL_CREDENTIAL_REJECTED"


def test_the_allow_list_still_rejects_everything_it_rejected_before() -> None:
    """Guards against a widened allow-list swallowing the existing #866/#949 boundary (e.g.
    matching on a prefix instead of an exact new member)."""
    assert _extract(_body(error_code="UNAUTHORIZED")) is None
    assert _extract(_body(error_code="TOTALLY_MADE_UP")) is None
    assert _extract(_body(error_code="model_credential_rejected")) is None  # case-sensitive


def test_nothing_but_the_code_is_carried() -> None:
    out = _extract(
        json.dumps(
            {
                "error_code": "MODEL_CREDENTIAL_REJECTED",
                "msg": "provider rejected the connected key sk-or-v1-abc123",
                "stack": "Traceback (most recent call last): ...",
            }
        ).encode()
    )
    assert out == "MODEL_CREDENTIAL_REJECTED"
    assert isinstance(out, str)
