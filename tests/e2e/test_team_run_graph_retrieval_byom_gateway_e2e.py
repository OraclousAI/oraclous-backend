"""BYOM in-loop graph retrieval END-TO-END through the GATEWAY — the cloud-first product surface.

#509 + #524 (E6 / ADR-040 Decision 7 — cloud-first / graph-primary). Proves the headline cloud loop:
a user imports an agent whose declared FILE tool (``Read``) is remapped onto the seeded GRAPH
capability (``core/knowledge-retriever``), binds the run to one of their graphs ONCE (``graph_id``),
and a REAL member mid-loop calls the retriever — which reaches the knowledge-retriever-service over
the org-trust path, searches the BOUND graph, and feeds the hit back into the agent's context. The
model never invents a UUID (graph_id is bound, optional in the tool schema); the agent only supplies
a query.

No fakes: real registration → real JWT → real credential → real engine → real worker → real LIVE
harness → real OpenRouter call → real knowledge-retriever-service over the bound graph. The graph is
created AND seeded with a unique marker through the gateway (``POST /api/v1/graphs`` +
``/api/v1/graphs/{id}/ingest``); only a real in-loop retrieval of the bound graph surfaces the
marker in the team result.

Requires (same as the other BYOM tests):
  - the harness in LIVE mode  (HARNESS_LLM_MODE=live — ``scripts/e2e.sh --byom``)
  - OPENROUTER_API_KEY in the env (the user's BYOM key)
Skipped otherwise, so it never reddens the deterministic suite or unit CI.
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

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")  # the user's own key, provided via env
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


def _researcher_studio(root: Path, query_hint: str) -> None:
    """A one-member 'graph researcher' studio. Its only tool is ``Read`` → remapped to the graph
    retriever under the cloud-first default. The body tells it to fulltext-search the (pre-bound)
    graph and report what it finds — so a SUCCEEDED live run carries proof of a real retrieval."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "You are a graph researcher. Use your Read tool (it searches the knowledge graph) to find "
        f"information about: {query_hint}. Call the tool with a `query` and `mode` of `hybrid`; "
        "the graph is already selected for this run, so do NOT pass a graph_id. Then reply with "
        "the EXACT codename string you retrieved, verbatim, and nothing else."
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


def _seed_graph(c: httpx.Client, marker: str) -> str:
    """Create a graph and ingest a unique-marker fact into it through the gateway; wait for the
    ingest job so the marker is fulltext-searchable when the agent retrieves it."""
    g = c.post("/api/v1/graphs", json={"name": "byom-retrieval-kb", "description": "in-loop e2e"})
    assert g.status_code == 201, f"create graph failed: {g.status_code} {g.text}"
    graph_id = g.json()["id"]
    text = f"The codename of the classified initiative is {marker}."
    job = c.post(f"/api/v1/graphs/{graph_id}/ingest", json={"content": text, "source_type": "text"})
    assert job.status_code == 202, f"ingest failed: {job.status_code} {job.text}"
    job_id = job.json()["id"]
    for _ in range(45):
        state = str(c.get(f"/api/v1/graphs/{graph_id}/jobs/{job_id}").json().get("status")).upper()
        if state in ("SUCCEEDED", "COMPLETED"):
            return graph_id
        if state in ("FAILED", "ERROR"):
            raise AssertionError(f"ingest job failed: {state}")
        time.sleep(2)
    raise AssertionError("ingest job never completed")


@requires_byom_key
def test_a_member_retrieves_from_the_bound_graph_mid_loop(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # a unique first token → the auth-service personal-org slug is unique (never piles onto a
    # shared, retry-exhaustible slug space across repeated e2e runs)
    user = register(f"graphbyom{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    # 1) the user stores THEIR OWN model token via the real credential API
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

    # 2) create + seed a graph with a unique marker (through the gateway, real KGS + ingest)
    marker = f"ZEPHYR-{uuid.uuid4().hex[:8]}"
    graph_id = _seed_graph(c, marker)

    # 3) import the cloud-first researcher (Read → core/knowledge-retriever); point it at BYOM
    _researcher_studio(tmp_path, query_hint="the classified initiative codename")
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    assert set(sub_harnesses) == {"researcher"}
    # the remap put the graph retriever on the member's ceiling, not the file sandbox
    caps = {c2["binding"]: c2["ref"] for c2 in sub_harnesses["researcher"]["capabilities"]}
    assert caps.get("Read") == "core/knowledge-retriever@1.0.0", caps
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]

    # 4) run the team bound to the graph THROUGH THE GATEWAY — real worker → live harness
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
    assert done["state"] == "SUCCEEDED", done
    # only a genuine in-loop retrieval of the BOUND graph can surface the seeded marker
    assert marker in str(done["results"]), (
        f"marker {marker!r} not in results — did the member retrieve from the bound graph? "
        f"results={done['results']!r}"
    )
