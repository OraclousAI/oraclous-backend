"""#694 — a team declaring a file tool is filed on the GRAPH instead, proven live and KEYLESS.

Its sibling (`test_saved_team_agents_and_binding_gateway_e2e.py`) names `graph-ingest` directly, so
it proves a member REACHES the graph but never exercises the correction itself. This file declares
`write` — the per-organisation tmp directory that team run `fe548aac` put ~10 KB into while its
bound graph stayed empty — and proves the correction fires on the deployed stack.

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
