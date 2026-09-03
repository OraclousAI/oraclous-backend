"""#907 — the new op_drafter_simulated/reviewer_output_simulated tokens reach the client unchanged.

``team_draft_routes.py:51`` already turns a ``TeamRunError.error_type`` into
``{"loc": ["body"], "type": exc.error_type}`` on the 422 it raises, and
``domain/validation_passthrough.extract_validation_details`` (this module) already turns THAT into
``details[0].issue`` through the existing leak-safe hole (#225/#281) — no gateway code change. This
test pins that the new token specifically survives the hole, so a future change to the sanitiser
(the charset, the cap, the loc-to-field fallback) cannot silently start dropping it.

This test is expected to PASS on day one — the hole was built generically (any Pydantic-shaped
``{"loc": [...], "type": "..."}`` item), not against a token allow-list, so nothing here needs the
[impl] to land. That is the point: it is the proof that #907's harness-runtime-service +
execution-engine-service fix (services/harness-runtime-service and services/execution-engine-service
``test_simulated_run_visibility.py`` / ``test_team_draft_simulated_op_drafter.py``, RED today) needs
NO corresponding gateway change — only the upstream error_type has to start being emitted.
"""

from __future__ import annotations

import json

import pytest
from oraclous_application_gateway_service.domain.validation_passthrough import (
    extract_validation_details,
)
from oraclous_errors import FieldError

pytestmark = pytest.mark.unit


def _upstream_422(error_type: str) -> bytes:
    # The exact shape team_draft_routes.py:51 emits from a TeamRunError: {"loc": ["body"], "type":
    # exc.error_type} — `msg` is never surfaced by extract_validation_details, so its content here
    # is immaterial; a real one would carry the (leak-unsafe) developer-facing message.
    return json.dumps(
        {"detail": [{"loc": ["body"], "type": error_type, "msg": "irrelevant, never surfaced"}]}
    ).encode()


def test_op_drafter_simulated_survives_the_passthrough_as_an_uppercase_token() -> None:
    details = extract_validation_details(_upstream_422("op_drafter_simulated"))
    assert details == [FieldError(field="body", issue="OP_DRAFTER_SIMULATED")]


def test_reviewer_output_simulated_survives_the_passthrough_as_an_uppercase_token() -> None:
    details = extract_validation_details(_upstream_422("reviewer_output_simulated"))
    assert details == [FieldError(field="body", issue="REVIEWER_OUTPUT_SIMULATED")]
