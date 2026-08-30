"""#697 / #751 — a compiled team's members hand each other NAMED results, through the gateway.

Drives the DEPLOYED stack on `:8006` with a real registration, a real JWT and the user's own
OpenRouter key — no fakes (CLAUDE.md §9, FUCK_CLAUDE_FUCK_PAPERCLIP rule 5). The compile leg needs a
real model (the fake client emits prose, never the compiled JSON), so it rides the `byom` marker:

    scripts/e2e.sh --byom

What it proves, on a team nobody hand-edited:

- every member of the compiled draft declares what it hands on (#697) — the property that was
  `{}` on all 14 members of run `fe548aac` and on all 5 members of the 2026-08-29 repro;
- dependencies are ROLE NAMES, not positions (#751) — the field that cost compiler run
  `2d24b128` its whole team.
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
_MODEL = "openrouter/openai/gpt-4o-mini"
_OBJECTIVE = (
    "Research this week's most-cited AI papers, then write a short plain-text digest of them."
)


def _model(cred_id: str) -> dict:
    return {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": cred_id},
    }


def _cred(c: httpx.Client, user: dict) -> str:
    r = c.post(
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
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _poll(c: httpx.Client, run_id: str, budget: float = 900.0) -> dict:
    deadline = time.time() + budget
    row: dict = {}
    while time.time() < deadline:
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"}:
            return row
        time.sleep(5)
    return row


@requires_byom
@pytest.mark.byom
def test_a_compiled_team_declares_what_each_member_hands_on(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"handoff{uuid.uuid4().hex[:8]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    gid = c.post("/api/v1/graphs", json={"name": "named-handoff"}).json()["id"]

    compiled = c.post(
        "/v1/engine/compiler-runs",
        json={"objective": _OBJECTIVE, "models": [_model(cred)], "graph_id": gid},
    )
    assert compiled.status_code == 202, compiled.text
    run = _poll(c, compiled.json()["id"])
    assert run["state"] == "SUCCEEDED", f"the compiler team must run — {run}"

    seeded = c.post(
        "/v1/engine/team-drafts/from-run",
        json={"team_run_id": run["id"], "name": "named-handoff team"},
    )
    assert seeded.status_code == 201, seeded.text
    envelope = seeded.json()
    assert envelope["would_block"] is False, envelope
    members = envelope["draft"]["manifest"]["members"]
    assert members, "the compiled team has members"

    for member in members:
        # #697: every member, with no exception for one nobody depends on.
        declared = member.get("outputs_schema") or {}
        assert declared.get("required"), f"{member['role']} declares no output: {declared}"
        # #751: dependencies are role names. An index here is what dropped run 2d24b128.
        for dep in member.get("depends_on") or []:
            assert isinstance(dep, str), f"{member['role']} depends on {dep!r}, not a role name"

    roles = {m["role"] for m in members}
    for member in members:
        for dep in member.get("depends_on") or []:
            assert dep in roles, f"{member['role']} depends on unknown role {dep!r}"
