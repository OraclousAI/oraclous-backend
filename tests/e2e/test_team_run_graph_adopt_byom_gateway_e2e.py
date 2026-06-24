"""Graph-adopt team-scope blackboard END-TO-END through the GATEWAY (#513, E6 / ADR-027 reshape).

Proves the cloud-first graph-adopt guarantee: a team bound to the user's EXISTING graph uses THAT
graph as its team-scope memory blackboard — members write ``scope=team`` ``:Memory`` into the
adopted graph, the team's reads see them, and the platform stands up **NO second graph** (no
auto-created ``agent_memory`` org-default). The team identity is the stable team-manifest id (every
member shares it), so the blackboard is the team's, not a lone agent's.

No fakes: real registration → real BYOM credential → real engine → real worker → real LIVE harness →
real OpenRouter → real KGS memory writes/reads, all through the gateway. The adopted graph is made
through the gateway (``POST /api/v1/graphs``); the assertions read back through the gateway only.

Requires (like the other BYOM tests): harness LIVE (``scripts/e2e.sh --byom``) + OPENROUTER_API_KEY.
Skipped otherwise.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom_key = pytest.mark.skipif(
    _USER_MODEL_KEY is None, reason="OPENROUTER_API_KEY not set (BYOM real-LLM run)"
)
_MEMORY_GRAPH_NAME = (
    "Agent Memory"  # DEFAULT_MEMORY_GRAPH_NAME — the org-default no-2nd-graph rule forbids
)


def _byom_model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _two_member_team(root: Path, nonce: str) -> None:
    """A scout → synthesist team (synthesist depends_on scout). Each replies with the nonce, so each
    completes (SUCCEEDED) and the post-run hook writes one team-scope memory per member."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = f"Reply with exactly this token and nothing else: {nonce}"
    (agents / "scout.md").write_text(f"---\nname: scout\nmodel: sonnet\n---\n{body}\n")
    (agents / "synthesist.md").write_text(f"---\nname: synthesist\nmodel: sonnet\n---\n{body}\n")
    (root / "teams" / "1-scout").mkdir(parents=True)
    (root / "teams" / "1-scout" / "charter.md").write_text(
        "# Team I — Scout\n## Roster\n| Agent | Type | Model | Job |\n"
        "| --- | --- | --- | --- |\n| `scout` | subagent | sonnet | scout |\n"
    )
    (root / "teams" / "2-synth").mkdir(parents=True)
    (root / "teams" / "2-synth" / "charter.md").write_text(
        "# Team II — Synth\n## Roster\n| Agent | Type | Model | Job |\n"
        "| --- | --- | --- | --- |\n| `synthesist` | subagent | sonnet | synthesise |\n"
    )


def _poll(c: httpx.Client, run_id: str, until: set[str], tries: int = 45) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


def _graph_names(c: httpx.Client) -> list[str]:
    resp = c.get("/api/v1/graphs")
    assert resp.status_code == 200, resp.text
    return [g["name"] for g in resp.json()]


@requires_byom_key
def test_a_team_uses_the_adopted_graph_and_creates_no_second_graph(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # unique first token → unique personal-org slug (avoids the shared slug-space exhaustion)
    user = register(f"graphadopt{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "my openrouter model",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _USER_MODEL_KEY},
        },
    )
    assert cred.status_code == 201, cred.text
    credential_id = cred.json()["id"]

    # the user's EXISTING graph — the one the team must adopt as its blackboard
    g = c.post("/api/v1/graphs", json={"name": "my-world-model", "description": "adopted"})
    assert g.status_code == 201, g.text
    adopted_graph_id = g.json()["id"]
    before = _graph_names(c)
    assert _MEMORY_GRAPH_NAME not in before  # clean start

    # import the team, point each member at BYOM, run it BOUND to the adopted graph
    nonce = uuid.uuid4().hex[:10]
    _two_member_team(tmp_path, nonce)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    assert set(sub_harnesses) == {"scout", "synthesist"}
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": sub_harnesses,
            "gate_decisions": {},
            "graph_id": adopted_graph_id,
        },
    )
    assert created.status_code == 202, created.text
    done = _poll(c, created.json()["id"], {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED", done
    assert set(done["results"]) == {"scout", "synthesist"}

    # let the fire-and-forget team-scope memory writes land
    time.sleep(4)

    # (1) NO SECOND GRAPH: the team wrote into the adopted graph, not a new org-default memory graph
    after = _graph_names(c)
    assert _MEMORY_GRAPH_NAME not in after, (
        f"a second (org-default) graph was created: {after} — graph-adopt must write IN the "
        f"adopted graph only"
    )

    # (2) TEAM-SCOPE BLACKBOARD: the adopted graph now holds team-scope memory the team reads back
    ctx = c.get(
        f"/api/v1/graphs/{adopted_graph_id}/memories/context",
        params={"query": "team run outcome scout synthesist", "scope": "team", "max_tokens": 2000},
    )
    assert ctx.status_code == 200, ctx.text
    block = ctx.json()["context_block"]
    assert block.strip(), (
        "the adopted graph holds no team-scope memory — the blackboard never wrote"
    )
