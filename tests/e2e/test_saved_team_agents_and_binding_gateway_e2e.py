"""#878 — a run resolves a saved team's filed agents and binds them with the caller's model.

The half of the journey that does NOT depend on a model drafting a team. It hand-authors the team,
which is the same `POST /v1/engine/team-drafts` a console user hits when they build one rather than
compile one, and it is what makes this a gate rather than a coin flip: no drafter means no tool
roulette, one member means no cascade, and the only model turn is the member doing its own job.

The FILING half of #695 R8 — the agent is listed and its reference resolves — needs no model and is
gated keyless in `test_saved_team_file_tool_substrate_gateway_e2e.py`. This file keeps only what a
real model is genuinely required for, so the key-gated leg stays as small as it can be.

What it proves, through the application-gateway on `:8006` only, with a real model:

* **#878 (ruled shape A)** — the draft no longer carries its agents inline (ADR-050 D3), so the run
  posts `sub_harnesses={}` and every member is RESOLVED from its reference. The model binding rides
  on `manifest.models[]` alone and the engine threads it onto each resolved member. Without that,
  every member fails `502: live LLM mode requires a model in the OHM`.

Supplying documents in `sub_harnesses` would defeat the whole point: an inline sub-harness wins, the
resolution seam is never reached, and the binding branch never executes. That mistake is what the
first version of this proof made.

The compile-from-prose leg of #694 G6 is proven separately by a local run pasted on PR #879. It is
not committed as a gate because the drafter ignores the objective's tool list about one run in four
(#883), and a test that fails one run in four teaches people to ignore CI.

Nothing is injected server-side and nothing is asserted against the database: the graph is created
fresh per run through the public API, so anything served for it came from THIS run. Auto-skips
without the gateway or the BYOM key, and a skip is not a pass.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"

#: the one seeded graph write capability — where a member's deliverable belongs under the cloud
#: default (ADR-040 D7 / ADR-041 D3). The team declares it by name; the engine resolves the ref.
_GRAPH_INGEST = "core/graph-ingest@1.0.0"


def _model_doc(cred_id: str) -> dict[str, Any]:
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
    return str(r.json()["id"])


def _team(org: str, nonce: str) -> dict[str, Any]:
    """One agent that writes one short note to the team's shared knowledge graph.

    Deliberately the smallest team that still exercises everything under test: it is an agent (so
    it is filed), it declares a graph capability (so #694's substrate correction applies to it),
    and it has real work to do (so a model must actually run for it to succeed)."""
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "saved-team-binding",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": "writer",
                "kind": "agent",
                # what a hand-authored team carries before it is saved: no registry id yet, so
                # filing mints one. The assertions below are that it stops looking like this.
                "manifest_ref": "org:x/writer@1",
                "subgoal": (
                    "Write two or three sentences on why a small team benefits from writing its"
                    f" decisions down. Include the exact token {nonce} verbatim. Save the result to"
                    " the team's shared knowledge graph with your graph-ingest tool — that is the"
                    " deliverable, and you are not finished until it is saved."
                ),
                "depends_on": [],
                "tools": ["graph-ingest"],
            }
        ],
        "runtime": {"entrypoint": "writer"},
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _connect(c: httpx.Client, tool: str) -> None:
    """Connect a tool for the organisation, as the console's connect step does.

    A capability the organisation has not instantiated fails the dispatch closed, so a saved team is
    not runnable until its user connects what it declared."""
    rows = c.get("/api/v1/capabilities", params={"kind": "tool"}).json()["capabilities"]
    cap = next((r for r in rows if _slug(str(r["name"])) == tool), None)
    assert cap is not None, f"{tool!r} is not a registered capability in this organisation"
    created = c.post(
        "/api/v1/instances",
        json={"capability_id": cap["id"], "name": tool, "configuration": {}, "settings": {}},
    )
    assert created.status_code == 201, created.text


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_byom
def test_a_saved_team_files_its_agents_and_runs_them_with_the_callers_model(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    nonce = f"nonce-{uuid.uuid4().hex[:12]}"
    user = register(f"savedteam{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)

    # ── 1. save the team ─────────────────────────────────────────────────────────────────────
    saved = c.post(
        "/v1/engine/team-drafts",
        json={"name": "saved team", "manifest": _team(user["org_id"], nonce), "sub_harnesses": {}},
    )
    assert saved.status_code == 201, saved.text
    envelope = saved.json()
    assert envelope["would_block"] is False, envelope["blocking"]
    draft = envelope["draft"]

    # ADR-050 D3 — one source of truth: the generated body is NOT kept beside the reference
    assert draft["sub_harnesses"] == {}, draft["sub_harnesses"]

    # The FILING half — the agent is listed, its reference resolves, and a re-save refreshes it —
    # needs no model, so it is gated keyless in
    # ``test_saved_team_file_tool_substrate_gateway_e2e.py`` rather than repeated here behind a key.
    # This test keeps only what a real model is actually required for.
    ref = str(draft["manifest"]["members"][0]["manifest_ref"])
    assert "org:x/" not in ref, f"the member still carries its pre-save ref {ref!r}"

    # ── 3. #878: run it EXACTLY as the console posts it — nothing inline ──────────────────────
    _connect(c, "graph-ingest")
    manifest = dict(draft["manifest"])
    manifest["models"] = [_model_doc(cred)]
    gid = c.post("/api/v1/graphs", json={"name": "saved-team-deliverable"}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": manifest,
            # EMPTY, and that is the point: the member must be resolved from its reference, and
            # the binding must reach it from manifest.models[] alone. Supplying a document here
            # makes the inline one win and the seam under test is never reached.
            "sub_harnesses": {},
            "gate_decisions": {},
            "graph_id": gid,
        },
    )
    assert created.status_code == 202, created.text
    done = _poll(c, str(created.json()["id"]))

    # asserted BEFORE the terminal state and independently of it, so a run that fails for any
    # other reason cannot mask the one thing this test exists to prove
    assert "requires a model in the OHM" not in str(done.get("error_message") or ""), (
        f"the resolved member was dispatched with no model — the binding did not reach it: {done}"
    )
    assert done["state"] == "SUCCEEDED", f"the saved team must run — {done}"

    # ── 4. the deliverable is on the bound graph, read back through the public API ────────────
    arts = c.get(f"/v1/artifacts?graph_id={gid}")
    assert arts.status_code == 200, arts.text
    listed = arts.json()
    assert listed, f"the bound graph holds nothing for a SUCCEEDED run — {done}"
    served = [c.get(f"/v1/artifacts/{a['id']}").json() for a in listed]
    assert any(a.get("content") for a in served), f"nothing served verbatim off the graph: {listed}"

    # RULE 8: only a real model carries the per-run token through. A fake-mode run cannot produce
    # it anywhere, so this is the proof the harness was LIVE.
    assert nonce in str(done["results"]) + str(served), (
        f"token {nonce!r} in no member result and on no artifact — was the harness LIVE?"
    )
