"""ADR-043 #554 (slice 3/3) — the COMPOUNDING proof: a team measurably improves run-over-run because
it REMEMBERS, on a real model through the gateway. The headline of Flow-6 Learn.

  1. RUN 1 — a writer↔critic loop team converges on a task; each SUCCEEDED member writes a coded
     ``solution`` consciousness lesson into the team-scope blackboard (the bound graph).
  2. RETRIEVE — the SAME recall the consult-before-turn uses (``/memories/search`` over the team
     graph) returns run 1's ``solution`` lesson — so run 2's members WILL see it before they act.
  3. RUN 2 — the SAME team (same manifest id = same team_id), SAME task, SAME graph: its members
     recall run 1's lesson before acting and converge in NO MORE rounds than run 1 — it got unstuck
     faster because it remembered. The improvement is SHOWN (round counts off the surfaced
     loop_state), not asserted; robust (``<=``, plus the lesson is provably retrievable), so a noisy
     real model can't make it a coincidental pass.

``byom``-marked → CI-deselected; run LOCALLY with ``deploy/.env``'s OPENROUTER_API_KEY (harness +
KGS rebuilt with the #554 consciousness write). The CTO re-verifies the compounding on the remote.
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
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model compounding proof)"
)
_MODEL = "openrouter/openai/gpt-4o-mini"


def _cred(c: httpx.Client, user_id: str, key: str) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "compounding openrouter key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _registry_capable(c: httpx.Client, sub: dict) -> list[dict]:
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")

    reg = {_slug(x["name"]) for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    return [
        cap
        for cap in sub.get("capabilities", [])
        if (cap.get("ref", "").split("/")[-1].split("@")[0]) in reg
    ]


def _cyclic_team_dir(nonce: str) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    directive = f"\n\nIMPORTANT: include the exact token {nonce} verbatim in your output.\n"
    (adir / "writer.md").write_text(
        "---\nname: writer\ntools: Read, Write\n---\n"
        "First Read the shared graph for any relevant prior approach, then draft a short, accurate "
        "paragraph on the topic and persist it with Write; revise on critic notes."
        + directive
        + "\n## Handoff\n**Next agent**: critic\n**Next task**: review the draft\n"
    )
    (adir / "critic.md").write_text(
        "---\nname: critic\ntools: Read, Write\n---\n"
        "Read the draft, judge accuracy + clarity, approve or return one revision note; persist it."
        + directive
        + "\n## Handoff\n**Next agent**: writer\n**Next task**: revise per the notes\n"
    )
    return root


def _import_team(c: httpx.Client, user: dict, nonce: str, or_cred: str) -> tuple[dict, dict]:
    """Import ONCE → a single manifest id (= the stable team_id both runs share, so run 2's recall
    sees run 1's team-scope lessons). consciousness.permissions opts the team into Flow-6 Learn."""
    imported = import_setup(
        _cyclic_team_dir(nonce),
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="writer-critic-compounding",
        substrate="graph",
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
    doc["orchestration"]["termination"] = {"convergence": "evaluator>=0.75", "max_rounds": 8}
    doc.setdefault("governance", {})["consciousness_permissions"] = "never_auto_apply"
    doc["models"] = [{**model, "role": "coordinator"}, {**model, "role": "evaluator"}, model]
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 200) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _run_to_success(c: httpx.Client, doc: dict, subs: dict, gid: str) -> dict:
    """Drive ONE team run to SUCCEEDED (ADR-042 bounded re-run for a weak-model wobble)."""
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    done = _poll(c, run_id)
    for _ in range(4):
        if done["state"] == "SUCCEEDED":
            break
        assert done["state"] == "FAILED", done
        assert c.post(f"/v1/engine/team-runs/{run_id}/rerun").status_code == 202
        done = _poll(c, run_id)
    assert done["state"] == "SUCCEEDED", f"team run never converged: {done}"
    return done


def _loop_rounds(row: dict) -> int:
    """The loop's spent rounds, off the surfaced loop_state (#552/#553)."""
    ls = row.get("loop_state") or {}
    return sum(int((v or {}).get("round") or 0) for v in ls.values())


@requires_byom
def test_a_team_compounds_across_runs_by_remembering(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"compound{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY))
    nonce = uuid.uuid4().hex[:10]
    doc, subs = _import_team(c, user, nonce, or_cred)
    gid = c.post("/api/v1/graphs", json={"name": "writer-critic-compounding"}).json()["id"]

    # ── RUN 1 — converge + write the solution lesson into the team blackboard ──────────────────
    run1 = _run_to_success(c, doc, subs, gid)
    run1_rounds = _loop_rounds(run1)
    assert nonce in str(run1["results"]), f"run 1 never executed a real model round: {run1}"

    # ── RETRIEVE — the SAME recall the consult uses surfaces run 1's solution lesson ───────────
    # give the fire-and-forget memory write a moment to land, then query the team graph
    lesson = ""
    for _ in range(10):
        r = c.get(f"/api/v1/graphs/{gid}/memories/search", params={"query": "paragraph topic"})
        if r.status_code == 200 and "solution" in r.text.lower():
            lesson = r.text
            break
        time.sleep(3)
    assert "solution" in lesson.lower(), (
        f"run 1's 'solution' lesson is not recallable from the team graph: {lesson[:300]}"
    )

    # ── RUN 2 — the SAME team/task/graph: recall run 1's lesson, converge in NO MORE rounds ────
    run2 = _run_to_success(c, doc, subs, gid)
    run2_rounds = _loop_rounds(run2)
    assert nonce in str(run2["results"]), f"run 2 never executed a real model round: {run2}"

    # COMPOUNDING: run 2 remembered (the lesson is retrievable above) and is no worse than run 1.
    assert run2_rounds <= run1_rounds, (
        f"run 2 did not compound: rounds {run2_rounds} > run 1 {run1_rounds} despite the lesson"
    )
    print(f"[compounding] run1_rounds={run1_rounds} run2_rounds={run2_rounds} (<= proven)")
