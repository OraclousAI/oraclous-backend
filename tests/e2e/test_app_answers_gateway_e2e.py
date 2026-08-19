"""#846 — a validation-desk intake answer reaches a real team member through the DEPLOYED stack.

ADR-052 decision 3: the app ships ``inputs["answers"]`` with a per-item ``hypothesis`` flag as a
one-off, so a founder's "I don't know" enters the run as an assumption to test rather than a
premise. Two things have to be true, and CI's unit suite can prove neither:

  GATE (no model key needed) — ``answers`` is accepted where it used to be a 422, while every OTHER
  undeclared key and every malformed ``answers`` payload is still refused. Driven through the
  gateway, so it also proves the engine's structured 422 survives the leak-safe passthrough.

  DELIVERY (real BYOM) — the flagged question and the directive that forbids treating it as given
  actually land in a real member's harness input, through engine → worker → live harness. The
  member is asked to echo its input verbatim, so the assertion is on TEXT THAT ARRIVED, never on a
  model's judgement.

No fakes, no internal port, no DB-direct assertions (FUCK_CLAUDE_FUCK_PAPERCLIP.md rule 5).
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"

_Q_SEGMENT = "Who is the target customer?"
_Q_STAGE = "What stage are you at?"
_ANSWERS = [
    {"question": _Q_SEGMENT, "answer": None, "hypothesis": True},
    {"question": _Q_STAGE, "answer": "pre-seed, two founders", "hypothesis": False},
]


def _team_manifest(org_id: str, models: list[dict] | None = None) -> dict:
    """A minimal one-member team that declares NO task_input and NO fan_out — the exact shape the
    validation desk runs, and the shape that used to 422 on any app field."""
    doc: dict = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "validation-desk-e2e",
            "owner_organization_id": org_id,
            "kind": "team",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/researcher@1",
                "subgoal": "research the demand signal",
            }
        ],
        "runtime": {"entrypoint": "researcher"},
    }
    if models is not None:
        doc["models"] = models
    return doc


def _issues(resp: httpx.Response) -> set[str]:
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_FAILED", err
    return {d["issue"] for d in (err.get("details") or [])}


def test_the_answers_field_is_accepted_and_the_gate_still_holds(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """One registration, three legs — the suite is already at its per-IP registration ceiling
    (#844), so these do not get a client each."""
    user = register(f"answers{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    manifest = _team_manifest(user["org_id"])

    # LEG 1 — the reported block is gone: the app's own field reaches the run.
    accepted = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": manifest,
            "sub_harnesses": {},
            "gate_decisions": {},
            "inputs": {"answers": _ANSWERS},
        },
    )
    assert accepted.status_code == 202, accepted.text

    # LEG 2 — a malformed payload is refused at create, never half-rendered (fail-closed, §3.5).
    malformed = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": manifest,
            "sub_harnesses": {},
            "gate_decisions": {},
            "inputs": {"answers": [{"question": _Q_SEGMENT, "answer": 50}]},
        },
    )
    assert malformed.status_code == 422, malformed.text
    assert "INVALID_ANSWERS" in _issues(malformed)

    # LEG 3 — acceptance criterion 2: nothing else about the undeclared-key gate moved.
    undeclared = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": manifest,
            "sub_harnesses": {},
            "gate_decisions": {},
            "inputs": {"answers": _ANSWERS, "pr_url": "https://example.invalid/1"},
        },
    )
    assert undeclared.status_code == 422, undeclared.text
    assert "UNDECLARED_INPUT_KEY" in _issues(undeclared)


def _credential(c: httpx.Client, user: dict) -> str:
    created = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "byom",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _echoing_team(user: dict, credential_id: str) -> tuple[dict, dict]:
    """Import a real one-member agent whose whole job is to echo the input it was handed, so the
    assertion lands on delivered text rather than on model judgement."""
    from oraclous_ohm.import_.setup import import_setup

    root = pathlib.Path(tempfile.mkdtemp())
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text(
        "---\nname: researcher\n---\n"
        "Reply with the EXACT text you were given, verbatim and in full. Add nothing.\n"
    )
    imported = import_setup(
        root, owner_organization_id=uuid.UUID(user["org_id"]), name="validation-desk-e2e"
    )
    model = {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }
    subs = {role: {**sub, "models": [model]} for role, sub in imported.sub_harnesses.items()}
    doc = imported.manifest.model_dump(mode="json")
    doc["models"] = [model]
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_byom
def test_a_flagged_answer_reaches_a_real_member_marked_unverified(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The proof the whole issue is about. A key that passes the gate but that nothing renders is
    the silent discard #714 closed — so this drives a LIVE harness and reads the member's own
    output back for the flagged question and the directive that frames it."""
    user = register(f"answersbyom{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    doc, subs = _echoing_team(user, _credential(c, user))

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": doc,
            "sub_harnesses": subs,
            "gate_decisions": {},
            "inputs": {"answers": _ANSWERS},
        },
    )
    assert created.status_code == 202, created.text
    done = _poll(c, created.json()["id"])
    assert done["state"] == "SUCCEEDED", f"the run must complete — {done}"

    echoed = str((done.get("results") or {}).get("researcher") or "")
    assert _Q_SEGMENT in echoed, f"the flagged question never reached the member — {echoed[:400]}"
    assert "UNVERIFIED ASSUMPTIONS" in echoed, (
        f"it arrived, but not marked as an assumption to test — {echoed[:400]}"
    )
    assert _Q_STAGE in echoed, f"the confirmed answer was dropped — {echoed[:400]}"
