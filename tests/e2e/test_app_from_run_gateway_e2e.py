"""#938 — a person turns their own finished run into an app, on the DEPLOYED stack.

#932 shipped the Apps tab with only Oraclous-provided apps in it, because turning a team into an
app was deferred. This is that path, and it is the first time an organisation puts its OWN team
behind a form.

Two claims here cannot be settled anywhere else, which is why they are proven through the gateway
against real services rather than in an integration test:

  THE FORM IS DRAFTED BY A REAL MODEL. A team declares exactly one input — the whole request as
  prose — so there are no field names to label. The fields are INVENTED by a model reading the
  team's description and the request this run was actually started with. A fake-mode draft would
  return whatever the fake was scripted to say and prove nothing (RULE 8), so this leg needs a real
  key.

  THE INVENTED FIELDS SURVIVE THE ENGINE'S OWN GATE. The engine fail-closes on any input key the
  manifest does not declare. So the drafted fields must be folded back into the team's one declared
  key before the run starts, and the only honest proof of that is a run that reaches SUCCEEDED
  through the real worker and the real harness.

The journey is one test on purpose. Every step needs the one before it — you cannot draft a form
without a finished run, and you cannot run the app without the form — and each registration costs
the shared per-IP rate limiter, which this suite has tripped before.

No fakes, no internal port, no DB-direct assertions (FUCK_CLAUDE_FUCK_PAPERCLIP.md rule 5). The
caller's model key is pasted in through the public credentials API moments before it is used.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"

#: The single key the team below declares. Everything a person types into the app's form ends up
#: under this one key, which is the whole reason the fold exists.
_TASK_KEY = "task"

#: The request the run is started with. The drafted form is read out of THIS text, so it is written
#: the way a person would write it: several distinct things in one paragraph.
_REQUEST = (
    "Write a competitor brief on Acme Cloud, focused on their pricing move last month. "
    "Keep it quick — one short paragraph, no more."
)


def _credential(c: httpx.Client, user: dict) -> str:
    created = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "e2e model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert created.status_code == 201, created.text
    assert _OR_KEY not in created.text, "the store response echoed the secret"
    return str(created.json()["id"])


def _model(credential_id: str) -> dict[str, Any]:
    """The caller's own binding. Built once and threaded everywhere, because the drafting call, the
    team run and the app run must all spend the SAME key the person just pasted in — and because a
    fresh credential per call would store three of them for one journey."""
    return {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _brief_team(user: dict, model: dict[str, Any]) -> tuple[dict, dict]:
    """A real one-member team that writes a short competitor brief from its request.

    It declares ``task_input`` explicitly. That is what a compiled team carries and what the fold
    targets — without it there is nowhere for the drafted fields to go, and the test would prove
    the fold against a shape no real team has.
    """
    from oraclous_ohm.import_.setup import import_setup

    root = pathlib.Path(tempfile.mkdtemp())
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "writer.md").write_text(
        "---\nname: writer\n---\n"
        "You write a very short competitor brief from the request you are handed.\n"
        "Name the competitor and the angle you were asked for, then stop. "
        "Two sentences at most, and never more.\n"
    )
    imported = import_setup(
        root, owner_organization_id=uuid.UUID(user["org_id"]), name="competitor-brief-e2e"
    )
    subs = {role: {**sub, "models": [model]} for role, sub in imported.sub_harnesses.items()}
    doc = imported.manifest.model_dump(mode="json")
    doc["models"] = [model]
    doc["task_input"] = {
        "required": True,
        "key": _TASK_KEY,
        "description": "The competitor to cover and the angle to take.",
    }
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _suggested_form(
    c: httpx.Client, run_id: str, models: list[dict[str, Any]], tries: int = 8
) -> list[dict[str, Any]]:
    """Ask for the drafted form, collecting it if the drafter outran the first call's budget.

    ``models`` is required on the FIRST call: drafting is a real model run on the caller's own key,
    and there is no platform fallback to borrow, so omitting it is a 409 rather than a draft. The
    collect call carries the token instead — by then the model is already chosen and running.

    202 with a collect token is not a failure — it is the same shape the intake read-back already
    uses for a model call that is slower than one HTTP request should wait for (#866).
    """
    body: dict[str, Any] = {"models": models}
    for _ in range(tries):
        resp = c.post(f"/v1/engine/team-runs/{run_id}/suggested-form", json=body)
        if resp.status_code == 200:
            return list(resp.json()["fields"])
        assert resp.status_code == 202, resp.text
        body = {"form_draft_run_id": resp.json()["form_draft_run_id"]}
        time.sleep(5)
    raise AssertionError("the form was never drafted")


@pytest.mark.byom
@requires_byom
def test_a_person_turns_their_finished_run_into_an_app_their_colleagues_can_run(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The whole issue, in the order a person lives it."""
    author = register(f"appfromrun{uuid.uuid4().hex[:10]} author")
    c = gateway_client(author["token"])

    # 1) run the team for real, and let it finish. An app is made from a run its author watched
    #    work — that is the ruling, and it is why nothing below can be reached without this.
    model = _model(_credential(c, author))
    doc, subs = _brief_team(author, model)
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": doc,
            "sub_harnesses": subs,
            "gate_decisions": {},
            "inputs": {_TASK_KEY: _REQUEST},
        },
    )
    assert created.status_code == 202, created.text
    run_id = str(created.json()["id"])
    finished = _poll(c, run_id)
    assert finished["state"] == "SUCCEEDED", f"the run never succeeded: {finished}"

    # 2) a REAL model reads that request and proposes the fields. The team declared one input, so
    #    every field here was invented — none of these names exists anywhere in the manifest.
    fields = _suggested_form(c, run_id, [model])
    assert fields, "the drafter proposed no fields at all"
    assert all(f["name"].strip() for f in fields), f"a field came back unnamed: {fields}"
    assert all(f["type"] in {"short_text", "long_text", "choice"} for f in fields), fields
    # What must not happen is the drafter degrading to the form #932 already had: one long-text
    # box carrying the team's own description. Asserted by SHAPE rather than by counting fields,
    # because how many a model finds in one paragraph is the model's judgement on the day — and a
    # test that fails on a reasonable answer is a flaky test, which is a bug (CLAUDE.md §11).
    assert not (len(fields) == 1 and fields[0]["type"] == "long_text"), (
        f"the drafter fell back to the un-drafted single box rather than reading the request: "
        f"{fields}"
    )

    # 3) the person edits a name before saving, and the app is stored with what they chose.
    edited = [{**f} for f in fields]
    edited[0]["name"] = "Company"
    saved = c.post(
        "/v1/engine/apps",
        json={
            "team_run_id": run_id,
            "name": "Competitor Brief",
            "description": "One page on a named competitor.",
            "fields": edited,
        },
    )
    assert saved.status_code == 201, saved.text
    app = saved.json()
    assert app["origin"] == "organisation"
    assert app["source_team_run_id"] == run_id
    assert app["form"][0]["name"] == "Company", "the person's edit was not what got stored"

    # 4) it is in the Apps tab, beside the Oraclous-provided one and told apart from it.
    listed = c.get("/v1/engine/apps").json()["apps"]
    mine = next((a for a in listed if a["id"] == app["id"]), None)
    assert mine is not None, "the app the person just made is not in their own Apps tab"
    assert any(a["origin"] == "platform" for a in listed), "the two kinds no longer both appear"

    # 5) a colleague fills the form in, and the invented fields reach the team as one request. The
    #    engine refuses any key the manifest does not declare, so SUCCEEDED here IS the fold.
    values = {f["id"]: (f.get("example") or "Acme Cloud") for f in app["form"]}
    started = c.post(
        f"/v1/engine/apps/{app['id']}/runs",
        json={
            "inputs": values,
            "models": [model],
        },
    )
    assert started.status_code == 202, started.text
    app_run_id = str(started.json()["id"])
    app_run = _poll(c, app_run_id)
    assert app_run["state"] == "SUCCEEDED", f"the app's own run never succeeded: {app_run}"

    # 6) the run belongs to this organisation, and appears in the app's own history.
    assert app_run["organisation_id"] == author["org_id"]
    history = c.get(f"/v1/engine/apps/{app['id']}/runs").json()["team_runs"]
    assert app_run_id in {r["id"] for r in history}

    # 7) an app an organisation made for itself is its own. Nobody else lists it, opens it, or sees
    #    that it was ever run — unlike the Oraclous-provided one, which is deliberately shared.
    stranger = gateway_client(register(f"appfromrun{uuid.uuid4().hex[:10]} stranger")["token"])
    assert app["id"] not in {a["id"] for a in stranger.get("/v1/engine/apps").json()["apps"]}
    assert stranger.get(f"/v1/engine/apps/{app['id']}").status_code == 404
    assert stranger.get(f"/v1/engine/apps/{app['id']}/runs").status_code == 404


def test_an_app_cannot_be_made_from_a_run_that_is_not_the_callers(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The cheap half, and it needs no model at all: a run id the caller cannot read is a 404, not
    a 403 that confirms the run exists."""
    who = register(f"appfromrun{uuid.uuid4().hex[:10]} nobody")
    c = gateway_client(who["token"])

    refused = c.post(
        "/v1/engine/apps",
        json={
            "team_run_id": str(uuid.uuid4()),
            "name": "Not Mine",
            "description": None,
            "fields": [{"name": "Topic", "hint": "", "type": "short_text"}],
        },
    )

    assert refused.status_code == 404, refused.text
