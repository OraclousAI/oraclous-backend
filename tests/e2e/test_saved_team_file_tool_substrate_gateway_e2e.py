"""#694 + #695 R8 — what a saved team files, proven live and KEYLESS.

Its sibling (`test_saved_team_agents_and_binding_gateway_e2e.py`) names `graph-ingest` directly, so
it proves a member REACHES the graph but never exercises the correction itself. This file declares
`write` — the per-organisation tmp directory that team run `fe548aac` put ~10 KB into while its
bound graph stayed empty — and proves the correction fires on the deployed stack.

It also carries the FILING half of #695 R8 — the agent is listed for the organisation, its
`manifest_ref` resolves through `GET /api/v1/capabilities/{id}`, and a re-save refreshes it rather
than minting a second one. None of that needs a model either, and a proof that needs no key belongs
in the leg CI actually runs.

Deliberately in its own module so it carries NO `byom` marker: it runs no model, so it belongs in
the deterministic leg CI actually runs, not the real-LLM leg CI skips for want of a key. That is
also why it is the half of #694 that can be a gate at all — the compile-from-prose half is hostage
to #883, where the drafter hands a member a tool the objective excluded about one compile in four.

Through the application-gateway on `:8006` only. Auto-skips when the gateway is down, and a skip is
not a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

#: the one seeded graph write capability — what a file sink is corrected onto (ADR-041 D3)
_GRAPH_INGEST = "core/graph-ingest@1.0.0"


def _library(c: httpx.Client) -> list[dict[str, Any]]:
    """The org's filed agents — what ``/app/agents`` reads."""
    got = c.get("/api/v1/capabilities", params={"kind": "harness"})
    assert got.status_code == 200, got.text
    rows: list[dict[str, Any]] = got.json()["capabilities"]
    return rows


def _team(org: str, tool: str) -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "declares-a-file-tool",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/writer@1",
                "subgoal": "write the note",
                "depends_on": [],
                "tools": [tool],
            }
        ],
        "runtime": {"entrypoint": "writer"},
    }


def test_a_saved_team_declaring_a_file_tool_is_filed_on_the_graph_instead(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """#694's substrate correction, proven live.

    Two things must both hold, and they look contradictory until you see which is which:

    * the verdict says BLOCKED — under the graph substrate a member does not get a file sink, and
      ``F-SUBSTRATE-FILE`` names the member and the tool so the reviewer can act on it;
    * the draft is still SAVED, and the agent filed for it carries the GRAPH ref. That is
      Amendment 2: a draft is repaired by being written, not by a migration, so the stored manifest
      describes what it actually uses at all times. A path that refuses cannot heal.

    Needs no model: nothing is run.
    """
    user = register(f"filetool{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])

    manifest = _team(user["org_id"], "write")
    saved = c.post(
        "/v1/engine/team-drafts",
        json={"name": "declares a file tool", "manifest": manifest, "sub_harnesses": {}},
    )
    assert saved.status_code == 201, saved.text
    envelope = saved.json()

    assert envelope["would_block"] is True, "a file sink under the graph substrate must block"
    blocking = " ".join(str(b) for b in envelope["blocking"])
    assert "F-SUBSTRATE-FILE" in blocking, blocking
    assert "writer" in blocking and "write" in blocking, blocking

    ref = str(envelope["draft"]["manifest"]["members"][0]["manifest_ref"])
    descriptor = c.get(f"/api/v1/capabilities/{ref}").json()["descriptor"]
    capability = descriptor["capabilities"][0]
    assert capability["ref"] == _GRAPH_INGEST, (
        f"the filed agent still points at a tmp sandbox: {descriptor['capabilities']}"
    )
    # the BINDING is preserved byte-for-byte (ADR-032, the ceiling is binding-based) — only the
    # ref moves, so the member's declared tools[] still matches what its agent holds
    assert capability["binding"] == "write", capability


def test_a_saved_team_files_one_agent_per_member_and_a_re_save_refreshes_it(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """#695 R8's filing half, which needs no model.

    A compiled member and a console-built agent are the same object, and only the console one was
    ever filed. The compiler stamped ``org:compiled/<role>@1``, which resolved to nothing, so the
    agents page was empty and the generated agents died with the run.
    """
    user = register(f"filing{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])

    saved = c.post(
        "/v1/engine/team-drafts",
        json={
            "name": "saved team",
            "manifest": _team(user["org_id"], "graph-ingest"),
            "sub_harnesses": {},
        },
    )
    assert saved.status_code == 201, saved.text
    envelope = saved.json()
    assert envelope["would_block"] is False, envelope["blocking"]
    draft = envelope["draft"]

    # ADR-050 D3 — one source of truth: the generated body is NOT kept beside the reference. Two
    # copies would mean editing the filed agent silently does not affect the team, which makes the
    # reuse this exists for cosmetic.
    assert draft["sub_harnesses"] == {}, draft["sub_harnesses"]

    ref = str(draft["manifest"]["members"][0]["manifest_ref"])
    assert "org:x/" not in ref, f"the member still carries its pre-save ref {ref!r}"
    assert ref in {str(r["id"]) for r in _library(c)}, (
        "the agent is not listed for the organisation — the agents page reads this"
    )

    resolved = c.get(f"/api/v1/capabilities/{ref}")
    assert resolved.status_code == 200, resolved.text
    descriptor = resolved.json()["descriptor"]
    assert descriptor["metadata"]["kind"] == "agent", descriptor["metadata"]
    assert descriptor["metadata"]["name"] == "writer", descriptor["metadata"]
    assert [cap["ref"] for cap in descriptor["capabilities"]] == [_GRAPH_INGEST], descriptor

    # Re-saving the SAME draft refreshes its agent rather than minting a second one beside it.
    # It has to be the same draft: a fresh POST has no stored draft to reuse an id from, so it
    # mints by design (an id on a request body is never trusted — the same-org overwrite guard).
    # An earlier version of this check POSTed a second time and counted two rows, which proved
    # nothing: the team builder mints a new ``metadata.id`` per call, so it was comparing two
    # different teams that had each correctly filed one agent.
    replaced = c.put(
        f"/v1/engine/team-drafts/{draft['id']}",
        json={"name": "saved team, renamed", "manifest": draft["manifest"], "sub_harnesses": {}},
    )
    assert replaced.status_code == 200, replaced.text
    assert str(replaced.json()["draft"]["manifest"]["members"][0]["manifest_ref"]) == ref
    assert {str(r["id"]) for r in _library(c)} == {ref}, "the re-save minted a second agent"
