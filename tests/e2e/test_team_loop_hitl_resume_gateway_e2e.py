"""ADR-043 #552 PR-C (step 6) — a looping team with a per-round HITL gate PAUSES, a human APPROVES
through the gateway, and the loop RESUMES + converges. The deployed proof for the conductor's
human-in-the-loop + checkpoint/resume: nothing mocked, real BYOM model, the gate honoured (no
auto-skip), the run durably PAUSED + resumed via the advance machinery.

  1. A writer↔critic loop is imported from two agents (the importer's SCC isolation); a
     ``kind:human`` GATE member is inserted into the loop (the book team's §22 GO gate).
  2. The run PAUSES before the loop's first round on the undecided gate (state PAUSED, paused_at).
  3. The human APPROVES via ``POST …/advance``; the run resumes, the bounded coordinator iterates
     writer↔critic on the REAL model, converges (coded done-check), artifacts land + serve.

``byom``-marked → DESELECTED in CI; run LOCALLY with ``deploy/.env``'s OPENROUTER_API_KEY (the
engine must be rebuilt with migration 0013 — the loop_state column). The CTO re-verifies remotely.
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
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model HITL/resume proof)"
)
_MODEL = "openrouter/openai/gpt-4o-mini"


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


def _gated_loop_team(c: httpx.Client, user: dict, nonce: str, or_cred: str) -> tuple[dict, dict]:
    """Import a writer↔critic loop, then INSERT a kind:human GO gate into the loop SCC."""
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    directive = f"\n\nIMPORTANT: include the exact token {nonce} verbatim in your output.\n"
    (adir / "writer.md").write_text(
        "---\nname: writer\ntools: Read, Write\n---\nDraft a short, accurate paragraph and persist "
        "it with your Write tool; revise it when the critic returns notes."
        + directive
        + "\n## Handoff\n**Next agent**: critic\n**Next task**: review the draft\n"
    )
    (adir / "critic.md").write_text(
        "---\nname: critic\ntools: Read, Write\n---\nRead the draft, judge accuracy + clarity, and "
        "approve or return one revision note; persist your review."
        + directive
        + "\n## Handoff\n**Next agent**: writer\n**Next task**: revise per the notes\n"
    )
    imported = import_setup(
        root,
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="writer-critic-gated",
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
    # insert the kind:human GO gate as a loop member (the per-round HITL gate)
    doc["members"].append({"role": "gate", "kind": "human", "human_role": "approver"})
    doc["orchestration"]["loops"][0]["members"].insert(0, "gate")
    doc["orchestration"]["loops"][0].setdefault("routing", {})["gate"] = "approve to continue"
    doc["orchestration"]["success_criteria"] = "a short, accurate, clear paragraph on the topic"
    doc["orchestration"]["termination"] = {"convergence": "evaluator>=0.8", "max_rounds": 6}
    doc["models"] = [{**model, "role": "coordinator"}, {**model, "role": "evaluator"}, model]
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
def test_looping_team_pauses_on_a_gate_then_a_human_approve_resumes_to_convergence(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"loophitl{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "hitl openrouter key")

    nonce = uuid.uuid4().hex[:10]
    doc, subs = _gated_loop_team(c, user, nonce, or_cred)
    gid = c.post("/api/v1/graphs", json={"name": "writer-critic-gated"}).json()["id"]

    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    # 1) the loop PAUSES before its first round on the undecided gate — no auto-skip
    paused = _poll(c, run_id)
    assert paused["state"] == "PAUSED", (
        f"the gated loop must PAUSE, not {paused['state']}: {paused}"
    )
    assert "gate" in (paused.get("paused_at") or []), paused

    # 2) the human APPROVES through the gateway → the run resumes
    adv = c.post(
        f"/v1/engine/team-runs/{run_id}/advance", json={"gate_decisions": {"gate": "approve"}}
    )
    assert adv.status_code == 202, adv.text

    # 3) the resumed loop iterates writer↔critic on the REAL model + converges (ADR-042: re-run any
    # member a weak model fails, bounded, until SUCCEEDED)
    done = _poll(c, run_id)
    for _ in range(4):
        if done["state"] == "SUCCEEDED":
            break
        assert done["state"] == "FAILED", done
        rr = c.post(f"/v1/engine/team-runs/{run_id}/rerun")
        assert rr.status_code == 202, rr.text
        done = _poll(c, run_id)
    assert done["state"] == "SUCCEEDED", (
        f"the loop did not converge after the gate approval: {done}"
    )

    member_status = done.get("member_status") or {}
    assert member_status.get("gate") == "succeeded", member_status  # the gate decision was recorded
    assert not any(s in ("failed", "blocked") for s in member_status.values()), member_status
    assert nonce in str(done["results"]), "nonce in no result — was the harness LIVE?"

    arts = c.get(f"/v1/artifacts?graph_id={gid}")
    assert arts.status_code == 200, arts.text
    served = [c.get(f"/v1/artifacts/{a['id']}").json() for a in arts.json()]
    assert any(b.get("content") for b in served), f"no artifact landed after resume: {arts.json()}"
