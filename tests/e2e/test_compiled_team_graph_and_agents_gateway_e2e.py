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
    "or push it anywhere outside Oraclous. Use ONLY these tools and no others: web-research, "
    "graph-ingest, knowledge-retriever. Give no member any other tool for any reason."
)

#: The tools this test's user can actually connect: one needs their search key, the rest need none.
#: Naming them in the objective is what a real user does when their organisation has connected a
#: known set — and it is what keeps this proof deterministic. Left open, the drafter reached for a
#: delivery connector in one run out of three (``github-sink``), which needs a credential the test
#: has no way to supply, so the run died on a missing key rather than on anything under test.
_CONNECTABLE = {"web-research", "graph-ingest", "knowledge-retriever", "find-similar", "bash"}


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
        # A silent skip inside a DoD proof makes a green run mean less than it looks: the team
        # would run with a member's tool unconnected and the test would still report PASS.
        assert cap is not None, (
            f"the compiled team declared {tool!r}, which the registry does not carry —"
            f" it cannot be connected, so this run would not prove what it claims"
        )
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

    # ── 4. run the saved team on a FRESH graph, EXACTLY as the console posts it ──────────────
    # ``sub_harnesses`` is EMPTY and stays empty, and that is the whole point. The draft no longer
    # carries its agents inline (ADR-050 D3), so the console has nothing to put there and every
    # member must be RESOLVED from its manifest_ref at run creation. An earlier version of this
    # test supplied documents here; an inline sub-harness wins, so the resolution seam was never
    # reached and #878's binding branch never executed.
    #
    # The model binding therefore rides on ``manifest.models[]`` alone, and the engine threads it
    # onto each resolved member (#878, ruled shape A). Without it every member fails
    # 502 "live LLM mode requires a model in the OHM", which is what this stack did before the
    # ruling landed.
    declared = {t for m in members for t in (m.get("tools") or [])}
    # A tool outside the connectable set needs a credential this test cannot supply, so the run
    # would die on a missing key rather than on anything under test. The objective names the
    # allowed tools; a compile that ignores it is a defect worth failing on, LOUDLY, rather than
    # a red run that reads as if the substrate or the binding were broken.
    assert declared <= _CONNECTABLE, (
        f"the compiled team declared {sorted(declared - _CONNECTABLE)}, which the objective"
        f" excluded and this test cannot connect — re-run, or widen _CONNECTABLE deliberately"
    )
    _connect_tools(c, user, declared)

    nonce = f"nonce-{uuid.uuid4().hex[:12]}"
    manifest = dict(draft["manifest"])
    manifest["models"] = [_model_doc(cred)]
    # read the filed agents ONLY to assert what they were granted — none of this is posted
    granted_by_role = {
        role: c.get(f"/api/v1/capabilities/{ref}").json()["descriptor"]
        for role, ref in refs.items()
    }
    gid = c.post("/api/v1/graphs", json={"name": "compiled-team-deliverables"}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": manifest,
            "sub_harnesses": {},  # nothing inline: every member resolves from its reference
            "gate_decisions": {},
            "graph_id": gid,
            # RULE 8's marker leads, verbatim and alone, the way the doefin proof weaves it into
            # each agent's prompt. Buried at the end of a long objective a small model drops it,
            # and a dropped marker reads as "the harness was fake" when it only means the model
            # skimmed.
            "inputs": {
                "task": (
                    f"IMPORTANT: include the exact token {nonce} verbatim in your output.\n\n"
                    f"{_OBJECTIVE}"
                )
            },
        },
    )
    assert created.status_code == 202, created.text
    run_id = str(created.json()["id"])
    done = _poll(c, run_id)

    # #878 shape A, asserted BEFORE the terminal state and independently of it. This is the whole
    # point of the ruling, and it must not be masked by a run that failed for some other reason:
    # every member resolved from its reference got the caller's model, or none of them did.
    assert "requires a model in the OHM" not in str(done.get("error_message") or ""), (
        f"a resolved member was dispatched with no model — the binding did not reach it: {done}"
    )

    # a real model is graded on evidence it cites, and it sometimes cites a call it did not make.
    # The established affordance for that here is a re-run, which re-drives ONLY the failures
    # (the same shape test_doefin_team_byom_graph uses).
    for _ in range(3):
        if done["state"] == "SUCCEEDED":
            break
        assert done["state"] == "FAILED", done  # only a FAILED run is re-runnable
        assert c.post(f"/v1/engine/team-runs/{run_id}/rerun").status_code == 202
        done = _poll(c, run_id)
    assert done["state"] == "SUCCEEDED", f"the saved team must run — {done}"

    # #694: every member was granted a GRAPH capability, never a tmp-sandbox file tool. Read off
    # the filed agents through the public API — the same documents the run dispatched.
    granted = {
        role: [cap["ref"] for cap in granted_by_role[role].get("capabilities", [])] for role in refs
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

    # RULE 8: only a real model carries the per-run token through. A fake-mode run cannot produce
    # it anywhere, so this is the proof the harness was LIVE. Both surfaces count — a member's
    # answer or what it persisted — because which one carries it depends on where the model chose
    # to put it, and a re-run re-drives members whose earlier answer is then replaced.
    assert nonce in str(done["results"]) + str(served), (
        f"token {nonce!r} in no member result and on no artifact — was the harness LIVE?"
    )
