"""ORA-54 — the §3 sensitive-data rules: no forbidden substrings ever reach a response body.

§3 dominant risk is info-disclosure via error bodies leaking:

1. Stack traces, exception class names, file paths, line numbers
2. Internal hostnames, IPs, ports, service/container names
3. SQL, ORM errors, DB constraint names
4. Tokens, secrets, API keys
5. Framework/library names + versions (fingerprinting)
6. PII or the raw offending value reflected back
7. Auth differential messaging (user-vs-password)
8. Raw upstream error bodies

ORA-53 published a regex catalogue covering all eight rules in
``packages/errors/contract/forbidden-substrings.json`` and a scanner in
``tools.contract.error_envelope.scan_forbidden``. The scanner is the single
source of truth for what counts as a leak; this suite drives the gateway
through scenarios where each leak class *could* surface, and asserts none do.

Crucially: the probe ``/__probe__/raise-unhandled`` raises a bare ``Exception``
whose message intentionally contains content from every category above. If the
gateway's error middleware ever serialises ``str(exc)`` into the response —
the most common mistake — every category trips the scanner and the test
fails loudly with the rule numbers.

Marked ``security`` (§3's dominant risk is sensitive-data leakage) so it runs
in the security CI job alongside the rest of the threat-tagged suites; also
``unit`` so it runs on every PR.

RED until the gateway error middleware (a) maps unhandled exceptions to
canonical curated messages without ``str(exc)`` reaching the body, and (b)
never echoes caller-supplied content.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import CANONICAL_EXCEPTION_NAMES, EXPECTED_CODES

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _scan(body_text: str, patterns: list[Any]) -> list[str]:
    from tools.contract.error_envelope import scan_forbidden

    return scan_forbidden(body_text, patterns)


# --- the leak-trap probe: bare Exception with multi-category leaky message ---


def test_unhandled_exception_body_contains_no_forbidden_substrings(
    client: Any, forbidden_patterns: list[Any]
) -> None:
    """The probe's exception message contains stack/secret/SQL/host content.

    If translation reflects ``str(exc)`` into the envelope, the scanner trips.
    The test asserts the response body is clean.
    """
    response = client.post("/__probe__/raise-unhandled")
    body_text = response.text
    hits = _scan(body_text, forbidden_patterns)
    assert not hits, f"unhandled-exception body leaked forbidden patterns: {hits}\nbody={body_text}"


# --- caller-controlled inputs (path, headers, body) must not be reflected ---


def test_caller_supplied_authorization_header_is_not_reflected(
    client: Any, forbidden_patterns: list[Any]
) -> None:
    """A poisoned ``Authorization`` header MUST NOT survive into the body."""
    response = client.post(
        "/__probe__/raise/Unauthenticated",
        headers={"Authorization": "Bearer FAKETOKENabcdef0123456789TESTONLY"},
    )
    hits = _scan(response.text, forbidden_patterns)
    assert not hits, f"poisoned Authorization header leaked into body: {hits}"


def test_caller_supplied_body_value_is_not_reflected_on_validation_failed(
    client: Any, forbidden_patterns: list[Any]
) -> None:
    """The §3 ``details[].issue`` rule: never echo the raw offending value.

    Driving the framework-native validation pipeline with a poisoned body must
    not result in the offending string appearing anywhere in the envelope.
    """
    response = client.post(
        "/__probe__/echo",
        json={"value": "Bearer FAKETOKENabcdef0123456789TESTONLY"},
        headers={"Content-Type": "application/json"},
    )
    # whether the impl treats this as VALIDATION_FAILED, MALFORMED_REQUEST or
    # passes-through 200 is up to the impl's body schema; we only assert the
    # leak rule, which holds in all of those cases.
    hits = _scan(response.text, forbidden_patterns)
    assert not hits, f"caller body value leaked into response: {hits}"


# --- every canonical-exception probe response is clean ---------------------


_BESPOKE = {"VALIDATION_FAILED", "UNAUTHORIZED"}


@pytest.mark.parametrize("code", [c for c in EXPECTED_CODES if c not in _BESPOKE])
def test_default_constructed_probe_body_is_clean(
    code: str, client: Any, forbidden_patterns: list[Any]
) -> None:
    """No canonical-exception default-construct surfaces a forbidden pattern.

    Catches the regression where a hard-coded curated ``message`` accidentally
    contains a tripping substring (e.g. ``"FastAPI 0.115"`` for fingerprinting,
    or ``"sqlalchemy"`` in a constant).
    """
    cls_name = CANONICAL_EXCEPTION_NAMES[code]
    response = client.post(f"/__probe__/raise/{cls_name}")
    hits = _scan(response.text, forbidden_patterns)
    assert not hits, f"{code} default body tripped patterns: {hits}\nbody={response.text}"


def test_validation_failed_probe_body_is_clean(client: Any, forbidden_patterns: list[Any]) -> None:
    response = client.post("/__probe__/raise-validation")
    hits = _scan(response.text, forbidden_patterns)
    assert not hits, f"validation-failed probe tripped patterns: {hits}"


def test_unauthorized_probe_body_is_clean(client: Any, forbidden_patterns: list[Any]) -> None:
    for query in ("read_permitted=true", "read_permitted=false"):
        response = client.post(f"/__probe__/raise-unauthorized?{query}")
        hits = _scan(response.text, forbidden_patterns)
        assert not hits, f"unauthorized probe ({query}) tripped patterns: {hits}"


# --- framework-native error paths must also be clean -----------------------


def test_unknown_route_404_body_is_clean(client: Any, forbidden_patterns: list[Any]) -> None:
    """Framework's default 404 must be replaced by the canonical envelope.

    The default FastAPI/Starlette ``{"detail": "Not Found"}`` would itself pass
    the forbidden-substring scan but it would FAIL the schema. Conversely a
    sloppy custom handler that includes ``Traceback`` etc. would pass the
    schema but fail this scan. Both checks together pin the requirement.
    """
    response = client.get("/this-route-does-not-exist-anywhere-12345")
    hits = _scan(response.text, forbidden_patterns)
    assert not hits, f"unknown-route 404 body tripped patterns: {hits}"


def test_method_not_allowed_body_is_clean(client: Any, forbidden_patterns: list[Any]) -> None:
    response = client.get("/__probe__/raise/InternalError")  # probe routes are POST
    hits = _scan(response.text, forbidden_patterns)
    assert not hits, f"method-not-allowed body tripped patterns: {hits}"


def test_malformed_json_body_is_clean(client: Any, forbidden_patterns: list[Any]) -> None:
    response = client.post(
        "/__probe__/echo",
        content=b"{not json at all",
        headers={"Content-Type": "application/json"},
    )
    hits = _scan(response.text, forbidden_patterns)
    assert not hits, f"malformed-json body tripped patterns: {hits}"


# --- sanity: the scanner itself catches what it's supposed to ------------


def test_scanner_self_check_on_known_leaky_string(
    forbidden_patterns: list[Any],
) -> None:
    """Belt + braces: if this fails, ``scan_forbidden`` is broken and every
    other assertion in this file is falsely passing."""
    leaky = json.dumps(
        {
            "stack": "Traceback (most recent call last):",
            "secret": "password=hunter2",
            "host": "auth-svc.svc.cluster.local",
            "sql": "SELECT id FROM users",
            "token": "Bearer abcdef0123456789EXAMPLE",
        }
    )
    hits = _scan(leaky, forbidden_patterns)
    assert hits, "scanner failed to catch a known-leaky string — fixture broken"
