"""#580 — a member that retrieves from an EMPTY graph DEGRADES (PARTIAL), it does not crash.

END-TO-END through the gateway (real BYOM). The exact #440 shape: a from-scratch team whose graph
has no data yet. A real member mid-loop calls the knowledge-retriever over the BOUND (empty) graph;
the retriever returns 200+[] (data-absence). Under #580 (ADR-021 degrade-not-crash) the connector
flags it ``data_absent``, the harness feeds the model a "no data, proceeding" note, the member
completes as a flagged ``member_status="partial"``, and the TEAM still completes (SUCCEEDED) — NOT a
FAILED cascade (pre-#580 the member churned → escalate → FAILED-blocked the team).

No fakes: real registration → JWT → credential → engine → worker → LIVE harness → real OpenRouter →
real knowledge-retriever-service over the bound empty graph. Auto-skips without the BYOM key/gateway
(a skip is NOT a pass). The structured ``retrieval.empty`` degradation alert (ADR-021 never-silent)
is asserted out-of-band against the harness logs in the PR proof.
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


def _byom_model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _researcher_studio(root: Path) -> None:
    """A one-member graph researcher whose only tool is ``Read`` → remapped to the graph retriever.
    Told to search the (pre-bound) graph; on an EMPTY graph the retrieval finds nothing."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "You are a graph researcher. Use your Read tool (it searches the knowledge graph) to find "
        "the project's launch date. Call the tool with a `query` and `mode` of `hybrid`; the graph "
        "is already selected for this run, so do NOT pass a graph_id. If nothing is found, say so "
        "briefly and stop — do not keep retrying."
    )
    (agents / "researcher.md").write_text(
        f"---\nname: researcher\nmodel: sonnet\ntools: Read\n---\n{body}\n"
    )
    (root / "teams" / "1-research").mkdir(parents=True)
    (root / "teams" / "1-research" / "charter.md").write_text(
        "# Team I — Research\n## Roster\n| Agent | Type | Model | Job |\n"
        "| --- | --- | --- | --- |\n| `researcher` | subagent | sonnet | research |\n"
    )


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 40) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


@requires_byom_key
def test_empty_graph_member_degrades_partial_team_completes(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"emptygraph{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    # the user stores THEIR OWN model token via the real credential API
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

    # create an EMPTY graph — NO ingest, so any retrieval against it returns data-absence (200+[])
    g = c.post("/api/v1/graphs", json={"name": "empty-kb", "description": "from-scratch, no data"})
    assert g.status_code == 201, f"create graph failed: {g.status_code} {g.text}"
    graph_id = g.json()["id"]

    # import the cloud-first researcher (Read → core/knowledge-retriever); point it at BYOM
    _researcher_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    caps = {x["binding"]: x["ref"] for x in sub_harnesses["researcher"]["capabilities"]}
    assert caps.get("Read") == "core/knowledge-retriever@1.0.0", caps
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]

    # run the team bound to the EMPTY graph THROUGH THE GATEWAY — real worker → live harness
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": sub_harnesses,
            "gate_decisions": {},
            "graph_id": graph_id,
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    # #580: the empty retrieval DEGRADED the member — it is flagged partial, and the team COMPLETES
    # (not a FAILED cascade). Pre-#580 it churned on empty → escalate → FAILED-blocked the team.
    assert done["state"] == "SUCCEEDED", f"the team must complete, not FAILED-cascade — {done}"
    assert done["member_status"].get("researcher") == "partial", (
        f"the empty-graph member must DEGRADE to partial (not succeeded, not failed) — {done}"
    )
