"""ORA-54 — every error response carries an opaque, uniquely-generated ``requestId``.

§3 requires:

* ``requestId`` matches ``^req_[0-9A-Za-z]+$`` (schema-enforced; restated here
  as a behavioural test so the impl can't bypass the schema by hand-rolling).
* It is the **only** trace exposed — it maps server-side to the full trace.
* It is server-authoritative; the client never controls it. A caller-supplied
  ``X-Request-Id`` header MUST NOT appear verbatim in the body (else the
  request-id becomes a tampering surface the FE could phish through).

The opacity invariant (no internal IDs leaking through) is covered by
``test_sensitive_data_never_leaks.py``. Here we cover format, uniqueness, and
isolation from caller-controlled input.

RED until the gateway error middleware generates a ``req_…`` id per request
and stamps it into every envelope.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def test_request_id_matches_canonical_pattern(client: Any, request_id_re: re.Pattern[str]) -> None:
    response = client.post("/__probe__/raise/InternalError")
    body = response.json()
    request_id = body["error"]["requestId"]
    assert request_id_re.match(request_id), request_id


def test_request_id_is_unique_per_request(client: Any) -> None:
    """Two back-to-back error responses must carry different ``requestId``s.

    This catches the regression where the impl stamps a process-wide constant
    (e.g. forgetting to call the generator inside the handler) — every request
    would tie back to the same server-side trace, defeating correlation.
    """
    a = client.post("/__probe__/raise/InternalError").json()["error"]["requestId"]
    b = client.post("/__probe__/raise/InternalError").json()["error"]["requestId"]
    assert a != b


def test_caller_supplied_request_id_is_ignored(client: Any, request_id_re: re.Pattern[str]) -> None:
    """A caller-supplied ``X-Request-Id`` header must not be reflected.

    §3: ``requestId`` is server-authoritative. If the gateway echoes a caller
    header verbatim, an attacker can plant arbitrary strings into the body
    (a phishing/log-injection surface).
    """
    poisoned = "req_PLANTED_BY_CALLER_xyz123"
    response = client.post(
        "/__probe__/raise/InternalError",
        headers={"X-Request-Id": poisoned},
    )
    body = response.json()
    assert body["error"]["requestId"] != poisoned, body
    # belt + braces: still matches the canonical pattern
    assert request_id_re.match(body["error"]["requestId"]), body
