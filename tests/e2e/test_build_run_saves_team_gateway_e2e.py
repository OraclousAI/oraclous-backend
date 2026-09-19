"""#1169: a build that succeeds is SAVED as a team by the engine, through the API GATEWAY, NO fakes.

Before the fix the team was written only by the console's ``POST /v1/engine/team-drafts/from-run``
call. A user who closed the tab after describing a team, or whose first build FAILED and whose
retry SUCCEEDED, was left with a finished build and no saved team. The engine now saves the
compiled team itself when a compile run settles SUCCEEDED, and the console's from-run call becomes
the already-contracted idempotent repeat (200, same draft) instead of the creator (201).

What this proves, driven exactly as a browser drives it, through ``:8006`` only, with a real JWT
from a real registration, the user's own model key pasted through the credentials API, nothing
mocked and nothing asserted against the database (rule 5):

* **AC2, closed tab** — the compile run reaches SUCCEEDED and the user NEVER calls from-run, yet a
  saved team appears in ``GET /v1/engine/team-drafts``. Exactly one.
* **AC3, no duplicate** — the console's from-run for that run answers 200 with the SAME draft id,
  every time, and the list total stays 1.
* **AC1, retry** — a build whose first attempt FAILS (the provider refuses the key), then succeeds
  on ``rerun`` after the user corrects the key, is saved with no from-run call at all.
* **regression** — an ordinary, non-compile team run that SUCCEEDS saves nothing.

These tests are RED on a stack that does not carry the fix: the first assertion that no draft ever
appears is the failure. Real-model legs — REQUIRE the harness in LIVE mode + OPENROUTER_API_KEY
(rule 8: a fake-mode run can never pass them). A skip is NOT a pass (rule 3):
    scripts/e2e.sh --byom   (flips the harness live, runs -m byom, restores fake)
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = os.environ["E2E_MODEL"]

_OBJECTIVE = "Research this week's most-cited AI papers and compile a short plain-text digest."

#: how long the settle-time save gets to land after the run reads SUCCEEDED. The save runs in the
#: worker right at the settle, so this is generous; it bounds the wait, it is not a sleep.
_SAVE_BUDGET_SECONDS = 120.0


def _model(cred_id: str) -> dict:
    return {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": cred_id},
    }


def _cred(c: httpx.Client, user: dict, api_key: str | None = None) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "byom",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": api_key or _OR_KEY},
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _poll(c: httpx.Client, run_id: str, tries: int = 160) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _drafts(c: httpx.Client) -> dict:
    r = c.get("/v1/engine/team-drafts")
    assert r.status_code == 200, r.text
    return r.json()


def _await_saved_team(c: httpx.Client, run_id: str) -> dict:
    """Poll the draft list (bounded) until the engine has saved this build's team; return the list.

    The user never calls from-run here, so the ONLY thing that can put a row in this list is the
    engine's own settle-time save."""
    deadline = time.monotonic() + _SAVE_BUDGET_SECONDS
    listing: dict = {}
    while time.monotonic() < deadline:
        listing = _drafts(c)
        if listing["total"] >= 1:
            return listing
        time.sleep(3)
    raise AssertionError(
        f"run {run_id} SUCCEEDED but no team was saved within {_SAVE_BUDGET_SECONDS:.0f}s and the"
        f" user never called from-run — the engine does not save a finished build: {listing}"
    )


def _compile(c: httpx.Client, cred: str) -> str:
    gid = c.post("/api/v1/graphs", json={"name": "build-saves-team"}).json()["id"]
    compiled = c.post(
        "/v1/engine/compiler-runs",
        json={"objective": _OBJECTIVE, "models": [_model(cred)], "graph_id": gid},
    )
    assert compiled.status_code == 202, compiled.text
    return str(compiled.json()["id"])


def _from_run(c: httpx.Client, run_id: str) -> httpx.Response:
    return c.post("/v1/engine/team-drafts/from-run", json={"team_run_id": run_id})


@requires_byom
@pytest.mark.byom
def test_a_finished_build_is_saved_with_the_tab_closed_and_from_run_is_a_repeat(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """AC2 + AC3: no from-run call, yet the team is saved; the console's later call is a 200."""
    user = register(f"closedtab{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    run_id = _compile(c, _cred(c, user))

    run = _poll(c, run_id)
    assert run["state"] == "SUCCEEDED", f"the compiler team must run — {run}"

    # AC2 — WITHOUT ever calling from-run, the engine saved the team.
    listing = _await_saved_team(c, run_id)
    assert listing["total"] == 1, listing
    saved_id = listing["team_drafts"][0]["id"]
    assert listing["team_drafts"][0]["member_count"] >= 1, listing

    # AC3 — the console's from-run finds the engine's draft: 200 (not 201), the SAME id.
    first = _from_run(c, run_id)
    assert first.status_code == 200, f"from-run must be the idempotent repeat: {first.text}"
    assert first.json()["draft"]["id"] == saved_id, first.text
    assert _drafts(c)["total"] == 1

    second = _from_run(c, run_id)
    assert second.status_code == 200, second.text
    assert second.json()["draft"]["id"] == saved_id, second.text
    assert _drafts(c)["total"] == 1  # still ONE draft for this build


@requires_byom
@pytest.mark.byom
def test_a_build_that_fails_then_succeeds_on_retry_is_saved(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """AC1: the first attempt fails because the provider refuses the key; the user corrects the
    key through the public credentials API and re-runs; the retry's success saves the team with no
    from-run call, and from-run then answers 200 with the same draft."""
    user = register(f"retrybuild{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    bogus_key = "sk-or-v1-" + uuid.uuid4().hex + uuid.uuid4().hex
    cred = _cred(c, user, api_key=bogus_key)
    run_id = _compile(c, cred)

    failed = _poll(c, run_id)
    assert failed["state"] == "FAILED", f"a refused key must fail the build — {failed}"
    assert _drafts(c)["total"] == 0, "a FAILED build must not save a team"

    # the user pastes the right key over the wrong one — the same PUT the console's key form makes
    fixed = c.put(
        f"/credentials/{cred}",
        json={
            "id": cred,
            "user_id": user["user_id"],
            "tool_id": str(uuid.uuid4()),
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert fixed.status_code == 200, fixed.text

    rerun = c.post(f"/v1/engine/team-runs/{run_id}/rerun")
    assert rerun.status_code == 202, rerun.text
    done = _poll(c, run_id)
    assert done["state"] == "SUCCEEDED", f"the retried build must run — {done}"

    # AC1 — the retry's success saved the team; no from-run call was ever made.
    listing = _await_saved_team(c, run_id)
    assert listing["total"] == 1, listing
    saved_id = listing["team_drafts"][0]["id"]

    repeat = _from_run(c, run_id)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["draft"]["id"] == saved_id, repeat.text
    assert _drafts(c)["total"] == 1


@requires_byom
@pytest.mark.byom
def test_an_ordinary_team_run_that_succeeds_saves_no_team(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Regression: only a COMPILE run saves a team. A hand-authored one-member team is saved once
    by the user, then run; its SUCCEEDED settle must not add a second draft."""
    user = register(f"ordinary{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)

    manifest = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "ordinary-team",
            "owner_organization_id": user["org_id"],
            "kind": "team",
        },
        "members": [
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/writer@1",
                "subgoal": "Write one plain sentence about why small teams write decisions down.",
                "depends_on": [],
                "tools": [],
                "outputs_schema": {"required": ["summary"]},
                "tool_rationale": {},
            }
        ],
        "runtime": {"entrypoint": "writer"},
    }
    saved = c.post(
        "/v1/engine/team-drafts",
        json={"name": "ordinary team", "manifest": manifest, "sub_harnesses": {}},
    )
    assert saved.status_code == 201, saved.text
    body = dict(saved.json()["draft"]["manifest"])
    body["models"] = [_model(cred)]
    assert _drafts(c)["total"] == 1

    go = c.post(
        "/v1/engine/team-runs",
        json={"manifest": body, "sub_harnesses": {}, "gate_decisions": {}},
    )
    assert go.status_code == 202, go.text
    done = _poll(c, str(go.json()["id"]))
    assert done["state"] == "SUCCEEDED", f"the ordinary team must run — {done}"

    # the settle-time save runs in the worker right at SUCCEEDED, so give it a bounded window to
    # (wrongly) land before concluding nothing was saved
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        assert _drafts(c)["total"] == 1, "an ordinary run's settle saved a team"
        time.sleep(3)
