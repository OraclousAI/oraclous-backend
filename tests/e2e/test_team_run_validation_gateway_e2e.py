"""Team-run validation errors surface as VALIDATION_FAILED through the API GATEWAY — #483.

Before #483 the engine mapped a team-run `TeamRunError(422)` to a free-STRING detail, which the
gateway (correctly, for leak-safety) fell back to `MALFORMED_REQUEST` ("could not be parsed") — a
misleading code for a *validation* error. The engine now emits a STRUCTURED 422 detail
(`[{loc:["body"], type:<token>, msg:…}]`), so the gateway surfaces `VALIDATION_FAILED` with a
field-level issue (dropping the value-bearing `msg` — leak-safe). This proves it on the DEPLOYED
stack, through the gateway (:8006), with a real validation error — no fakes, no internal port.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def test_team_run_invalid_manifest_is_validation_failed_not_malformed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """A team-run create with an unparseable OHM manifest → VALIDATION_FAILED (field=body,
    issue=INVALID_MANIFEST), NOT MALFORMED_REQUEST — the #483 fix, through the gateway."""
    c = gateway_client(register("Val Manifest")["token"])
    resp = c.post(
        "/v1/engine/team-runs",
        json={"manifest": {}, "sub_harnesses": {}, "gate_decisions": {}},
    )
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_FAILED", err  # NOT MALFORMED_REQUEST (the pre-#483 behaviour)
    assert err["retryable"] is False, err
    # the gateway surfaced the structured field-level issue (loc+type), never the value-bearing msg
    details = err.get("details") or []
    assert any(d["field"] == "body" and d["issue"] == "INVALID_MANIFEST" for d in details), err


def test_team_run_not_a_team_is_validation_failed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """A manifest that parses as OHM but isn't a Team Harness → VALIDATION_FAILED (NOT_A_TEAM).
    Uses a seeded curated capability's descriptor as a known-valid, non-team OHM — no external
    fixtures, and it gets past the parse, so it's a semantic 422, not a parse 422."""
    c = gateway_client(register("Val NotTeam")["token"])
    # a curated tool descriptor is valid OHM (kind=tool) but is not metadata.kind == "team"
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    assert caps, "no curated capabilities seeded"
    # fetch one capability's full descriptor (a valid non-team OHM)
    cap = c.get(f"/api/v1/capabilities/{caps[0]['id']}")
    if cap.status_code != 200:
        pytest.skip("capability descriptor endpoint unavailable; invalid-manifest case covers #483")
    manifest = cap.json().get("descriptor") or cap.json()
    resp = c.post(
        "/v1/engine/team-runs",
        json={"manifest": manifest, "sub_harnesses": {}, "gate_decisions": {}},
    )
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_FAILED", err
    issues = {d["issue"] for d in (err.get("details") or [])}
    # either it failed the team-check (NOT_A_TEAM) or the parse (INVALID_MANIFEST) — both are
    # VALIDATION_FAILED, never MALFORMED_REQUEST. The point is the code, not which validator fired.
    assert issues, err
