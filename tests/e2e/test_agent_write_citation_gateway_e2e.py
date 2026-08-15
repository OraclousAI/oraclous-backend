"""A member's own writing is cited as ``agent``, END-TO-END through the GATEWAY (#786) — NO fakes.

§CITE rev4 decision 7: content the agent itself wrote is marked as agent-generated. A file an agent
writes into a workspace carries ``source_system: "agent"``, never ``"upload"`` — otherwise an agent
can write a file, retrieve it and cite it, passing every rule while showing the reader something
that looks like external evidence.

A real user, through the application-gateway on ``:8006`` only. No service port, no ``/internal``,
no DB-direct assertion, nothing mocked or monkeypatched.

Two legs, because the rule has two halves and one of them is that nothing else moved:

* the PROOF (``byom``, a REAL model) — a one-member team writes into its bound graph with its
  ``Write`` tool, and the record reads back as ``agent``. This is the only leg where the producer
  comes from a genuine engine dispatch: ``team_run.py`` binds ``producer_kind: "team-member"`` onto
  the member's instance configuration, the same trusted path ``graph_id`` travels, so the model can
  neither supply it nor forge it. A per-run nonce proves the model was real (rule 8) — a fake-mode
  run cannot echo it, so a green run cannot be a fake-harness run.
* the CONTROL (key-free) — a person's own upload, with no source and no producer, still reads back
  as ``upload``. Decision 7 distinguishes who authored the content; it must not reclassify the human
  half.

The proof leg needs a real BYOM key and auto-skips without one. A skip is NOT a pass (rule 3): run
it locally with ``deploy/.env``'s OPENROUTER_API_KEY and a LIVE harness (``scripts/e2e.sh --byom``).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom_key = pytest.mark.skipif(
    _USER_MODEL_KEY is None, reason="OPENROUTER_API_KEY unset (the real-model agent-write proof)"
)

_MODEL = "openrouter/openai/gpt-4o-mini"


# --------------------------------------------------------------------------------------------
# Gateway helpers — every one a public API call the user themselves could make
# --------------------------------------------------------------------------------------------


def _new_graph(c: httpx.Client, name: str) -> str:
    g = c.post("/api/v1/graphs", json={"name": f"{name}-{uuid.uuid4().hex[:6]}"})
    assert g.status_code == 201, g.text
    return str(g.json()["id"])


def _await_cited_hits(c: httpx.Client, graph_id: str, query: str) -> list[dict]:
    """Retry the read until the freshly written chunks are visible, then return the CITED hits."""
    hits: list[dict] = []
    for _ in range(45):
        r = c.post("/v1/search/hybrid", json={"query": query, "graph_id": graph_id, "top_k": 25})
        assert r.status_code == 200, r.text
        hits = list(r.json())
        cited = [h for h in hits if h.get("citation")]
        if cited:
            return cited
        time.sleep(2)
    raise AssertionError(f"no hit carried a citation after the write; last hits={hits[:2]}")


# --------------------------------------------------------------------------------------------
# The proof — a real member, a real model, a real dispatch
# --------------------------------------------------------------------------------------------


def _archivist_studio(root: Path, nonce: str) -> None:
    """A one-member team whose only tool is ``Write``.

    Under the graph substrate the importer remaps ``Write`` onto ``core/graph-ingest``, so the
    member's output is persisted to the bound graph in-loop — which is exactly the write path
    decision 7 is about. The nonce has to survive verbatim into the persisted text, because it is
    both the retrieval key and the proof the model was live.
    """
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "You are an archivist. Write one short paragraph of your own about how a team keeps notes, "
        f"and include the exact token {nonce} verbatim in it. Then persist that paragraph with "
        "your Write tool so the rest of the team can read it. The graph is already selected for "
        "this run, so do NOT pass a graph_id. Finally, reply with the paragraph you persisted."
    )
    (agents / "archivist.md").write_text(
        f"---\nname: archivist\nmodel: sonnet\ntools: Write\n---\n{body}\n"
    )
    (root / "teams" / "1-archive").mkdir(parents=True)
    (root / "teams" / "1-archive" / "charter.md").write_text(
        "# Team I — Archive\n## Roster\n| Agent | Type | Model | Job |\n"
        "| --- | --- | --- | --- |\n| `archivist` | subagent | sonnet | archive |\n"
    )


def _registry_capable(c: httpx.Client, sub: dict) -> list[dict]:
    """Keep only sub-harness capabilities this platform actually seeded."""

    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")

    reg = {_slug(x["name"]) for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    return [
        cap
        for cap in sub.get("capabilities", [])
        if (cap.get("ref", "").split("/")[-1].split("@")[0]) in reg
    ]


def _poll(c: httpx.Client, run_id: str, tries: int = 90) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_byom_key
@pytest.mark.byom
def test_a_members_own_writing_is_cited_as_agent_not_as_an_upload(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"agentwrite{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])

    # 1) the user brings their own model token, through the real credential API
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
    credential_id = str(cred.json()["id"])

    # 2) import the archivist; the graph substrate puts the graph writer on its ceiling
    nonce = uuid.uuid4().hex[:10]
    _archivist_studio(tmp_path, nonce)
    imported = import_setup(
        tmp_path,
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="archive",
        substrate="graph",
    )
    assert imported.manifest is not None
    subs = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    assert set(subs) == {"archivist"}
    caps = {x["binding"]: x["ref"] for x in subs["archivist"]["capabilities"]}
    assert caps.get("Write") == "core/graph-ingest@1.0.0", caps
    model = {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }
    for sub in subs.values():
        sub["models"] = [model]
        sub["capabilities"] = _registry_capable(c, sub)

    # 3) run the team bound to a fresh graph — real engine → real worker → LIVE harness
    graph_id = _new_graph(c, "agent-write-kb")
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": subs,
            "gate_decisions": {},
            "graph_id": graph_id,
        },
    )
    assert created.status_code == 202, created.text
    run_id = str(created.json()["id"])

    done = _poll(c, run_id)
    assert done["state"] == "SUCCEEDED", done
    # RULE 8: only a real LLM echoes the per-run nonce — a fake-mode run cannot.
    assert nonce in str(done["results"]), (
        f"nonce {nonce!r} in no result — was the harness LIVE? results={done['results']!r}"
    )

    # 4) the engine bound the producer onto the write. Asserted separately from the citation so a
    #    failure says WHICH half broke: an unbound producer (#807) reads differently from a bound
    #    producer the minting never learned about.
    arts = c.get(f"/v1/artifacts?graph_id={graph_id}")
    assert arts.status_code == 200, arts.text
    kinds = {a.get("producer_kind") for a in arts.json()}
    assert "team-member" in kinds, f"the run bound no producer onto its write: {arts.json()}"

    # 5) THE POINT: the member's own writing is not passed off as a document a person brought.
    citation = _await_cited_hits(c, graph_id, nonce)[0]["citation"]
    assert citation["source_system"] == "agent", (
        "a member's own writing was recorded as an upload — the reader cannot tell "
        f"platform-authored content from a human-brought document: {citation}"
    )
    assert citation["url"] is None, "an agent citation has nothing outside the platform to open"
    assert citation["revision_kind"] == "content_hash"
    assert uuid.UUID(str(citation["source_id"])), "an agent citation resolves to its ingest job"
    assert citation["citation_id"].startswith("cit_")


# --------------------------------------------------------------------------------------------
# The control — a person's upload is a different act and keeps its own value
# --------------------------------------------------------------------------------------------


def test_a_persons_own_upload_still_reads_as_an_upload(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Decision 7 records WHO authored the content. A human upload is untouched by it."""
    user = register(f"humanupload{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    graph_id = _new_graph(c, "human-upload-kb")
    nonce = uuid.uuid4().hex[:8]

    job = c.post(
        f"/api/v1/graphs/{graph_id}/ingest",
        json={
            "content": f"A quarterly plan a person wrote and uploaded. Probe {nonce}.\n",
            "filename": "quarterly-plan.md",
            "source_type": "text",
        },
    )
    assert job.status_code == 202, job.text
    job_id = str(job.json()["id"])
    for _ in range(90):
        state = str(c.get(f"/api/v1/graphs/{graph_id}/jobs/{job_id}").json().get("status")).upper()
        if state in ("SUCCEEDED", "COMPLETED"):
            break
        if state in ("FAILED", "ERROR"):
            raise AssertionError(f"ingest job {job_id} failed: {state}")
        time.sleep(2)
    else:
        raise AssertionError(f"ingest job {job_id} never completed")

    citation = _await_cited_hits(c, graph_id, nonce)[0]["citation"]
    assert citation["source_system"] == "upload", citation
    assert citation["source_id"] == job_id, "an upload citation resolves to its ingest job"
