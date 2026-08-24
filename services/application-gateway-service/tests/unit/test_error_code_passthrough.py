"""#866 — the two read-back refusal codes survive the gateway's 4xx boundary.

The gateway never relays an upstream error body: it drains it and re-emits the canonical envelope
under the same status (Interface Contracts §3 rule 8), because an upstream body can carry a stack
trace, an internal host, or a SQL fragment. Two narrow holes already exist in that wall — the
field/issue pair on a 422 and the ``needs_credential`` token on a 409 — and #866 opens a third: an
ALLOW-LISTED error code, and nothing else, read from the upstream body.

The allow-list is the whole safety argument. Only ``MODEL_NOT_CONNECTED`` and ``IDEA_TOO_VAGUE``
cross; every other value falls back to today's status-derived envelope, so a compromised or buggy
upstream cannot pick which code the browser sees, and no free text ever rides through.

Marked ``unit`` and ``security`` — the risk this guards is a leak channel, not a feature.

The extractor is imported FUNCTION-LOCALLY (``.claude/rules/tests-seam-imports.md``): it does not
exist until the ``[impl]`` lands, and a module-level import would abort collection for every suite.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _extract(raw: bytes):  # noqa: ANN202 — the return type ships with the seam
    from oraclous_application_gateway_service.domain.validation_passthrough import (
        extract_error_code,
    )

    return extract_error_code(raw)


def _body(**kw: object) -> bytes:
    return json.dumps(kw).encode()


# --- the two allow-listed codes cross ---------------------------------------


def test_model_not_connected_crosses_the_boundary() -> None:
    assert _extract(_body(error_code="MODEL_NOT_CONNECTED")) == "MODEL_NOT_CONNECTED"


def test_idea_too_vague_crosses_the_boundary() -> None:
    assert _extract(_body(error_code="IDEA_TOO_VAGUE")) == "IDEA_TOO_VAGUE"


def test_the_code_is_read_from_a_fastapi_detail_wrapper_too() -> None:
    # A FastAPI ``HTTPException(detail={...})`` nests the payload one level down. The engine
    # raises through that path, so both shapes must be understood or the code is silently lost.
    raw = json.dumps({"detail": {"error_code": "IDEA_TOO_VAGUE"}}).encode()
    assert _extract(raw) == "IDEA_TOO_VAGUE"


# --- everything else does not ------------------------------------------------


def test_a_code_outside_the_allow_list_is_dropped() -> None:
    # An upstream must not be able to choose ANY taxonomy code for the browser. Even a real,
    # valid code that is not one of the two falls back to the status-derived envelope.
    assert _extract(_body(error_code="UNAUTHORIZED")) is None
    assert _extract(_body(error_code="INTERNAL_ERROR")) is None


def test_an_invented_code_is_dropped() -> None:
    assert _extract(_body(error_code="TOTALLY_MADE_UP")) is None


def test_a_lowercase_or_padded_code_is_not_normalised_into_a_match() -> None:
    # No case-folding, no stripping: a near-miss is a miss. Normalising would widen the hole for
    # no benefit — the engine emits the exact token.
    assert _extract(_body(error_code="idea_too_vague")) is None
    assert _extract(_body(error_code=" IDEA_TOO_VAGUE ")) is None


def test_a_non_string_code_is_dropped() -> None:
    assert _extract(_body(error_code=42)) is None
    assert _extract(_body(error_code=["IDEA_TOO_VAGUE"])) is None
    assert _extract(_body(error_code=None)) is None


def test_a_body_without_an_error_code_is_dropped() -> None:
    assert _extract(_body(detail="something went wrong")) is None
    assert _extract(b"") is None
    assert _extract(b"not json at all") is None
    assert _extract(json.dumps(["a", "list"]).encode()) is None


def test_an_oversized_body_is_never_parsed() -> None:
    # Mirrors the other extractors' 64KiB ceiling: an upstream must not be able to make the edge
    # parse an unbounded body on the error path.
    raw = json.dumps({"error_code": "IDEA_TOO_VAGUE", "pad": "x" * (64 * 1024)}).encode()
    assert _extract(raw) is None


def test_nothing_but_the_code_is_carried() -> None:
    # The extractor returns a token, never a structure — so there is no field a message, a URL,
    # or a stack trace could ride in on.
    out = _extract(
        json.dumps(
            {
                "error_code": "MODEL_NOT_CONNECTED",
                "message": "psycopg2 could not connect to db-primary.svc.cluster.local",
                "stack": "Traceback (most recent call last): ...",
            }
        ).encode()
    )
    assert out == "MODEL_NOT_CONNECTED"
    assert isinstance(out, str)
