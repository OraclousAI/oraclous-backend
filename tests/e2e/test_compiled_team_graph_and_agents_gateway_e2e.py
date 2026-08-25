"""#694 G6 + #695 R8 — a compiled team's work reaches the bound graph, and its agents survive it.

The whole journey a real user drives, through the application-gateway only, with a real model:
register → paste a model key → create a knowledge graph → describe a team in prose → save the
compiled team → find its agents in the library → run it → read its deliverables back off the graph.

What this replaces, from team run ``fe548aac`` (real org, 14 members, a graph bound):

* every member resolved to ``core/write@1`` and put ~10 KB in ``/tmp/oraclous-agent-sandbox/<org>/``
  while the bound graph kept only the 4 nodes the compiler team itself had written (#694);
* every member's ``manifest_ref`` was ``org:compiled/<role>@1``, which resolved to nothing, so the
  agents page was empty and the generated agents died with the run (#695).

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
_TAVILY = os.environ.get("TAVILY_API_KEY", "")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"

# Deliberately steered away from any DELIVERY connector: the drafter otherwise reaches for one
# (``github-sink`` on the first attempt), and a fresh org has no credential configured for it, so
# the run fails on a missing credential rather than on anything this slice is about.
_OBJECTIVE = (
    "Write a short plain-text briefing on why a small team should keep a written decision log. "
    "Use exactly two members: one researches the reasons, one writes the briefing. Keep it under "
    "300 words. The member who writes the briefing MUST save it to the team's shared knowledge "
    "graph with the graph-ingest tool — that is the deliverable. Do not publish, deliver, send, "
    "or push it anywhere outside Oraclous, and give no member a delivery or publishing tool."
)


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


def _poll(c: httpx.Client, run_id: str, tries: int = 200) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _connect_tools(c: httpx.Client, user: dict, tools: set[str]) -> None:
    """Connect each tool the compiled team declared, as the console's connect step does.

    A capability the organisation has not instantiated fails the dispatch closed ("the organisation
    has no configured instance of it"), so a compiled team is not runnable until its user connects
    what it declared. ``web-research`` additionally needs the user's own search key bound to that
    instance. Everything here goes through the public API; nothing is injected server-side."""
    by_slug = {
        _slug(str(row["name"])): row
        for row in c.get("/api/v1/capabilities", params={"kind": "tool"}).json()["capabilities"]
    }
    for tool in sorted(tools):
        cap = by_slug.get(tool)
        if cap is None:  # a tool with no registered capability cannot be connected
            continue
        inst = c.post(
            "/api/v1/instances",
            json={
                "capability_id": cap["id"],
                "name": tool,
                "configuration": {},
                "settings": {},
            },
        )
        assert inst.status_code == 201, inst.text
        if tool == "web-research" and _TAVILY:
            cred = c.post(
                "/credentials/",
                json={
                    "tool_id": cap["id"],
                    "user_id": user["user_id"],
                    "name": "search key",
                    "provider": "tavily",
                    "cred_type": "api_key",
                    "credential": {"api_key": _TAVILY},
                },
            )
            assert cred.status_code == 201, cred.text
            bound = c.post(
                f"/api/v1/instances/{inst.json()['id']}/configure-credentials",
                json={"credential_mappings": {"api_key": cred.json()["id"]}},
            )
            assert bound.status_code == 200, bound.text


@requires_byom
def test_a_compiled_team_writes_to_the_graph_and_its_agents_exist_afterwards(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"compiled{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)

    # ── 1. describe a team in prose (the Describe door), through the public compile endpoint ──
    compile_graph = c.post("/api/v1/graphs", json={"name": "compile-scratch"}).json()["id"]
    started = c.post(
        "/v1/engine/compiler-runs",
        json={
            "objective": _OBJECTIVE,
            "models": [_model_doc(cred)],
            "graph_id": compile_graph,
        },
    )
    assert started.status_code == 202, started.text
    compile_run = _poll(c, str(started.json()["id"]))
    assert compile_run["state"] == "SUCCEEDED", f"the compile must run end-to-end — {compile_run}"

    # ── 2. save the compiled team (#695: this is where its agents are filed) ─────────────────
    saved = c.post("/v1/engine/team-drafts/from-run", json={"team_run_id": compile_run["id"]})
    assert saved.status_code == 201, saved.text
    draft = saved.json()["draft"]
    verdict = saved.json()
    members = draft["manifest"]["members"]
    agent_roles = [m["role"] for m in members if m.get("kind", "agent") == "agent"]
    assert agent_roles, f"the compiled team has agent members — {members}"

    # #694: the members were granted GRAPH capabilities, never the tmp-sandbox file tools
    assert verdict["would_block"] is False, verdict["blocking"]

    # ── 3. #695 R8: the agents are in the library, and each reference resolves ───────────────
    library = c.get("/api/v1/capabilities", params={"kind": "harness"})
    assert library.status_code == 200, library.text
    filed = {str(row["id"]): row for row in library.json()["capabilities"]}
    assert filed, "the agents page is empty — nothing was filed for it to read"

    refs = {m["role"]: str(m["manifest_ref"]) for m in members if m["role"] in agent_roles}
    for role, ref in refs.items():
        assert "org:compiled/" not in ref, f"{role} still carries the unresolvable ref {ref!r}"
        assert ref in filed, f"{role}'s agent {ref} is not in the org's library"
        resolved = c.get(f"/api/v1/capabilities/{ref}")
        assert resolved.status_code == 200, resolved.text
        descriptor = resolved.json()["descriptor"]
        assert descriptor["metadata"]["kind"] == "agent", descriptor["metadata"]
        assert descriptor["metadata"]["name"] == role, descriptor["metadata"]

    # a second save files no duplicate (idempotent per (org, team_run_id))
    again = c.post("/v1/engine/team-drafts/from-run", json={"team_run_id": compile_run["id"]})
    assert again.status_code == 200, again.text
    reread = again.json()["draft"]["manifest"]["members"]
    assert {m["role"]: str(m["manifest_ref"]) for m in reread if m["role"] in refs} == refs

    # ── 4. run the saved team on a FRESH graph ───────────────────────────────────────────────
    # The user's model binding is applied per member, by reading each filed agent back through the
    # public capabilities API and writing the binding into it. That read is what a caller has to do
    # while the draft no longer carries the agents inline: the console's own binder walks
    # ``sub_harnesses``, which is now empty, so a saved team otherwise runs with no model and every
    # member fails 502 "live LLM mode requires a model in the OHM" (reproduced on this stack).
    # WHERE that binding belongs is a cross-repo shape and is not settled here — Contract #878.
    _connect_tools(c, user, {t for m in members for t in (m.get("tools") or [])})

    nonce = f"nonce-{uuid.uuid4().hex[:12]}"
    manifest = dict(draft["manifest"])
    manifest["models"] = [_model_doc(cred)]
    bound_subs: dict[str, Any] = {}
    for role, ref in refs.items():
        agent = c.get(f"/api/v1/capabilities/{ref}").json()["descriptor"]
        bound_subs[role] = {**agent, "models": [_model_doc(cred)]}
    gid = c.post("/api/v1/graphs", json={"name": "compiled-team-deliverables"}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": manifest,
            "sub_harnesses": bound_subs,
            "gate_decisions": {},
            "graph_id": gid,
            "inputs": {"task": f"{_OBJECTIVE} Include the token {nonce} verbatim in your answer."},
        },
    )
    assert created.status_code == 202, created.text
    done = _poll(c, str(created.json()["id"]))
    assert done["state"] == "SUCCEEDED", f"the saved team must run — {done}"

    # RULE 8: only a real model echoes the per-run token. A fake-mode run cannot, so it is no proof.
    assert nonce in str(done["results"]), (
        f"token {nonce!r} in no member result — was the harness LIVE? (fake = no proof)"
    )

    # #694: every member was granted a GRAPH capability, never a tmp-sandbox file tool. Read off
    # the filed agents through the public API — the same documents the run dispatched.
    granted = {
        role: [cap["ref"] for cap in bound_subs[role].get("capabilities", [])] for role in refs
    }
    flat = [ref for refs_ in granted.values() for ref in refs_]
    assert not [r for r in flat if r in ("core/write@1", "core/edit@1", "core/read@1")], (
        f"a member still holds a tmp-sandbox file tool — {granted}"
    )
    assert any("graph-ingest" in r for r in flat), f"no member can persist to the graph — {granted}"

    # ── 5. #694 G6: the deliverables are ON THE BOUND GRAPH, read back through the public API ──
    arts = c.get(f"/v1/artifacts?graph_id={gid}")
    assert arts.status_code == 200, arts.text
    listed = arts.json()
    assert listed, (
        "the bound graph holds nothing for a SUCCEEDED run — the deliverables went somewhere else,"
        " which is exactly what #694 reports"
    )
    served = [c.get(f"/v1/artifacts/{a['id']}").json() for a in listed]
    assert any(a.get("content") for a in served), f"nothing served verbatim off the graph: {listed}"
