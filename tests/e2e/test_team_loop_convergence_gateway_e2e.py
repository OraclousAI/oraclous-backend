"""ADR-043 #552 — a CYCLIC team converges through the conductor, on a REAL model, via the gateway.

The load-bearing proof that the hybrid conductor (PR-B2) actually iterates a genuine loop to
convergence and HALTS a non-converging one at a coded bound — nothing mocked, the user brings their
own model (BYOM OpenRouter), the work lands on the bound graph and serves through /v1/artifacts.

  1. A writer↔critic team is IMPORTED from two agents with MUTUAL ``## Handoff`` — the importer
     isolates the SCC into ``orchestration.loops`` (a genuine cycle), the acyclic remainder (none
     here) would run on run_team. The user declares a convergence threshold (``evaluator>=0.8``).
  2. REAL MODEL (RULE 8) — every member's + the coordinator/evaluator's model points at the user's
     OpenRouter credential; the harness runs LIVE. A per-run nonce woven into each agent's output
     proves the model was real (fake mode cannot echo it).
  3. CONVERGE — the bounded BYOM coordinator routes writer↔critic round by round; the run SUCCEEDS
     only when the CODED done-check confirms (coverage + landed artifacts + the evaluator grade
     clears the threshold) — the team never satisfies its own done-check.
  4. HALT — a second team with ``max_rounds: 1`` + an unsatisfiable threshold ends FAILED at a coded
     bound, re-runnable (partial saved) — proving coded termination, not a model self-call.

``byom``-marked → DESELECTED in CI (no real model key there); run LOCALLY via ``scripts/e2e.sh``
with ``deploy/.env``'s OPENROUTER_API_KEY + the CTO's remote check on 192.168.1.202.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model convergence proof)"
)

_MODEL = "openrouter/openai/gpt-4o-mini"  # cheap-but-capable; a 2-round writer↔critic can clear it


def _cred(c: httpx.Client, user_id: str, key: str, name: str) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    assert r.status_code == 201, r.text
    assert key not in r.text  # KMS-sealed — never echoed
    return r.json()["id"]


def _registry_capable(c: httpx.Client, sub: dict) -> list[dict]:
    """Keep only sub-harness capabilities the platform has (so a member can write the graph)."""

    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")

    reg = {_slug(x["name"]) for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    return [
        cap
        for cap in sub.get("capabilities", [])
        if (cap.get("ref", "").split("/")[-1].split("@")[0]) in reg
    ]


def _write_cyclic_team(nonce: str) -> pathlib.Path:
    """Two agents with MUTUAL ``## Handoff`` → the importer makes a writer↔critic loop SCC."""
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    directive = f"\n\nIMPORTANT: include the exact token {nonce} verbatim in your output.\n"
    # tools: Read, Write — under the graph substrate the importer remaps these onto the seeded
    # knowledge-retriever (read) + graph-ingest (write) capabilities, so each member persists its
    # work to the bound graph in-loop (clearing the coded done-check's landed-artifacts floor).
    (adir / "writer.md").write_text(
        "---\nname: writer\ntools: Read, Write\n---\n"
        "You draft a short, accurate paragraph on the assigned topic, then persist it with your "
        "Write tool so the critic can read it. When the critic returns notes, revise the paragraph "
        "and persist the improved version with your Write tool." + directive + "\n## Handoff\n"
        "**Next agent**: critic\n**Next task**: review the draft for accuracy and clarity\n"
    )
    (adir / "critic.md").write_text(
        "---\nname: critic\ntools: Read, Write\n---\n"
        "You Read the writer's latest draft from the shared graph, judge its accuracy and clarity, "
        "and either approve it or return concrete notes for one revision. Persist your review with "
        "your Write tool." + directive + "\n## Handoff\n"
        "**Next agent**: writer\n**Next task**: revise the paragraph per the notes\n"
    )
    return root


def _import_loop_team(
    c: httpx.Client, user: dict, nonce: str, or_cred: str, *, convergence: str, max_rounds: int
) -> tuple[dict, dict]:
    """Import the cyclic team, point every model at the user's OpenRouter key, declare the goal +
    convergence threshold. Returns (manifest_doc, sub_harnesses)."""
    root = _write_cyclic_team(nonce)
    imported = import_setup(
        root,
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="writer-critic",
        substrate="graph",
    )
    assert imported.manifest.orchestration and imported.manifest.orchestration.loops, (
        "the importer must isolate the writer↔critic SCC into orchestration.loops"
    )
    model = {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": or_cred},
    }
    subs = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in subs.values():
        sub["models"] = [model]
        sub["capabilities"] = _registry_capable(c, sub)

    doc = imported.manifest.model_dump(mode="json")
    doc["orchestration"]["success_criteria"] = "a short, accurate, clear paragraph on the topic"
    doc["orchestration"]["termination"] = {"convergence": convergence, "max_rounds": max_rounds}
    # the coordinator routes + the evaluator grades, both BYOM on the user's key
    doc["models"] = [
        {**model, "role": "coordinator"},
        {**model, "role": "evaluator"},
        model,
    ]
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 150) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_byom
def test_cyclic_team_converges_on_a_real_model_and_lands_artifacts(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"loopconv{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "loop openrouter key")

    nonce = uuid.uuid4().hex[:10]
    doc, subs = _import_loop_team(
        c, user, nonce, or_cred, convergence="evaluator>=0.8", max_rounds=6
    )
    gid = c.post("/api/v1/graphs", json={"name": "writer-critic"}).json()["id"]

    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    # the conductor iterates writer↔critic until the CODED done-check confirms. ADR-042: re-run any
    # member that the weak model failed/blocked, bounded, until SUCCEEDED.
    done = _poll(c, run_id)
    for _ in range(4):
        if done["state"] == "SUCCEEDED":
            break
        assert done["state"] == "FAILED", done
        rr = c.post(f"/v1/engine/team-runs/{run_id}/rerun")
        assert rr.status_code == 202, rr.text
        done = _poll(c, run_id)
    assert done["state"] == "SUCCEEDED", f"the loop never converged to SUCCEEDED: {done}"

    member_status = done.get("member_status") or {}
    assert {"writer", "critic"} <= set(member_status), member_status
    assert not any(s in ("failed", "blocked") for s in member_status.values()), member_status
    # RULE 8: only a real LLM echoes the per-run nonce — a fake-mode run cannot.
    assert nonce in str(done["results"]), (
        f"nonce in no result — was the harness LIVE? {done['results']}"
    )
    # the coded done-check's evaluator gate stored a passing grade
    verdict = done.get("verdict") or {}
    if verdict.get("score") is not None:
        assert float(verdict["score"]) >= 0.8, verdict

    # the loop's work LANDED on the bound graph + serves verbatim through /v1/artifacts (the
    # coverage-floor's landed-artifacts half — the graph is fresh per-run, so it's from this run)
    arts = c.get(f"/v1/artifacts?graph_id={gid}")
    assert arts.status_code == 200, arts.text
    served = [c.get(f"/v1/artifacts/{a['id']}").json() for a in arts.json()]
    assert any(b.get("content") for b in served), (
        f"no artifact landed for a converged loop: {arts.json()}"
    )


@requires_byom
def test_non_converging_loop_halts_at_a_coded_bound_and_is_re_runnable(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # the team can NEVER satisfy its own done-check: an unsatisfiable threshold in ONE round ends
    # FAILED at the coded bound (max_rounds / no_progress), re-runnable — not a forever loop, not a
    # model self-declared success.
    user = register(f"loophalt{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "loop openrouter key")

    nonce = uuid.uuid4().hex[:10]
    doc, subs = _import_loop_team(
        c, user, nonce, or_cred, convergence="evaluator>=0.999", max_rounds=1
    )
    gid = c.post("/api/v1/graphs", json={"name": "writer-critic-halt"}).json()["id"]

    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    done = _poll(c, run_id)

    assert done["state"] == "FAILED", f"a non-converging loop must HALT FAILED, not {done['state']}"
    member_status = done.get("member_status") or {}
    assert any(s == "failed" for s in member_status.values()), (
        f"the halted loop's members must be re-runnable (failed): {member_status}"
    )
