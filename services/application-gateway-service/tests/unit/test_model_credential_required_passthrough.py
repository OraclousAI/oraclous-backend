"""#949 ruling Q4 (C5, gateway half) — the retriever's new refusal code crosses the gateway's 4xx
boundary the same way `MODEL_NOT_CONNECTED`/`IDEA_TOO_VAGUE` already do (#866): an ALLOW-LISTED
`error_code`, read from the upstream body, and nothing else.

`extract_error_code` already exists and already works generically (`test_error_code_passthrough.py`
proves the mechanism); what is missing is `MODEL_CREDENTIAL_REQUIRED` in the allow-list itself
(`_RELAYABLE_CODES`). Without it, KRS's typed 422 (pinned in
`knowledge-retriever-service/tests/integration/test_semantic_credential_refusal.py`) is dropped at
the edge and the console gets the generic status-derived envelope — exactly the silent failure
#949 exists to remove.

Marked `unit` and `security`, mirroring `test_error_code_passthrough.py` — the risk here is a leak
channel, not a feature.
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


def test_model_credential_required_crosses_the_boundary() -> None:
    assert _extract(_body(error_code="MODEL_CREDENTIAL_REQUIRED")) == "MODEL_CREDENTIAL_REQUIRED"


def test_the_code_is_read_from_a_fastapi_detail_wrapper_too() -> None:
    """KRS raises via `HTTPException(status_code=422, detail={"error_code": ..., ...})` — the
    nested-under-`detail` shape, mirroring how the engine's #866 refusals arrive."""
    raw = json.dumps({"detail": {"error_code": "MODEL_CREDENTIAL_REQUIRED"}}).encode()
    assert _extract(raw) == "MODEL_CREDENTIAL_REQUIRED"


def test_the_allow_list_still_rejects_everything_it_rejected_before() -> None:
    """Guards against a widened allow-list swallowing the existing #866 boundary (e.g. matching
    on a prefix instead of an exact new member)."""
    assert _extract(_body(error_code="UNAUTHORIZED")) is None
    assert _extract(_body(error_code="TOTALLY_MADE_UP")) is None
    assert _extract(_body(error_code="model_credential_required")) is None  # case-sensitive


def test_nothing_but_the_code_is_carried() -> None:
    out = _extract(
        json.dumps(
            {
                "error_code": "MODEL_CREDENTIAL_REQUIRED",
                "msg": "meaning-based search needs the organisation's model credential",
                "stack": "Traceback (most recent call last): ...",
            }
        ).encode()
    )
    assert out == "MODEL_CREDENTIAL_REQUIRED"
    assert isinstance(out, str)


# --- the taxonomy itself ------------------------------------------------------------------------


def test_the_error_code_enum_carries_the_new_member() -> None:
    from oraclous_errors import ErrorCode  # noqa: PLC0415

    assert getattr(ErrorCode, "MODEL_CREDENTIAL_REQUIRED", None) is not None
    assert ErrorCode.MODEL_CREDENTIAL_REQUIRED.value == "MODEL_CREDENTIAL_REQUIRED"


def test_the_new_code_carries_a_422_curated_policy() -> None:
    """422, not 409: the org IS the caller (no cross-party conflict, unlike CREDENTIALS_REQUIRED's
    409), and the request itself is fine — it is the organisation's current configuration that is
    below the floor, mirroring IDEA_TOO_VAGUE's own 400-vs-422 reasoning."""
    from oraclous_errors import CODE_POLICY, ErrorCode, http_status_for  # noqa: PLC0415

    code = ErrorCode.MODEL_CREDENTIAL_REQUIRED
    assert http_status_for(code) == 422
    assert CODE_POLICY[code].retryable_default is False
    assert CODE_POLICY[code].default_message  # a real, non-empty curated message
