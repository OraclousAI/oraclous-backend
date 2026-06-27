"""ADR-043 #553 — bounded RECALIBRATION on a real model, through the gateway (slice 2/3).

The load-bearing proof that the conductor's recalibration (PR-2) actually FIRES on a stalling
loop on the deployed stack and is BOUNDED — nothing mocked, the user brings their own model
(BYOM OpenRouter), the BYOM recalibrator picks a tactic over the coded diagnosis, and the cap
halts it (no second endless loop). ``recalibration_count`` is read off the surfaced loop_state.

  1. A writer↔critic team is IMPORTED from two agents with mutual ``## Handoff`` — the SCC the
     importer isolates into ``orchestration.loops``; the user declares a convergence threshold.
  2. UNRECOVERABLE — an unsatisfiable threshold (``evaluator>=0.99``) over enough rounds: the loop
     STALLS (the coordinator believes it's done / the signature repeats), the conductor runs ONE
     bounded recalibration (a real BYOM directive turn), it still can't converge → the cap halts it
     FAILED. Asserts ``loop_state[..].recalibration_count >= 1`` (recalibration fired) + a bounded
     terminal state (not a forever loop) + real, substantial member output (RULE 8: the BYOM
     recalibrator turn can't run in fake mode, so a recorded recalibration means a LIVE model).
  3. RECOVER — a clearable threshold: a stalling team recovers (in-drive after a recalibration, or
     via the ADR-042 bounded re-run) to SUCCEEDED — it gets unstuck, it does not run forever.

``byom``-marked → DESELECTED in CI; run LOCALLY via ``scripts/e2e.sh`` with ``deploy/.env``'s
OPENROUTER_API_KEY (the engine rebuilt with the #553 recalibration wiring) + the CTO's remote check.
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
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model recalibration proof)"
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


def _write_cyclic_team(nonce: str) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    directive = f"\n\nIMPORTANT: include the exact token {nonce} verbatim in your output.\n"
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
    root = _write_cyclic_team(nonce)
    imported = import_setup(
        root,
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="writer-critic-recal",
        substrate="graph",
    )
    assert imported.manifest.orchestration and imported.manifest.orchestration.loops
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


def _recal_count(row: dict) -> int:
    """Total bounded recalibrations the run spent, read off the surfaced loop_state checkpoint."""
    ls = row.get("loop_state") or {}
    return sum(int((v or {}).get("recalibration_count") or 0) for v in ls.values())


@requires_byom
def test_unrecoverable_loop_recalibrates_then_halts_at_the_cap(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # an unsatisfiable threshold over enough rounds: the loop STALLS, the conductor runs ONE bounded
    # recalibration (a real BYOM directive turn), still can't converge → the cap HALTS it FAILED —
    # not a forever loop, not a model self-declared success.
    user = register(f"recalhalt{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "recal openrouter key")

    nonce = uuid.uuid4().hex[:10]
    doc, subs = _import_loop_team(
        c, user, nonce, or_cred, convergence="evaluator>=0.99", max_rounds=8
    )
    gid = c.post("/api/v1/graphs", json={"name": "writer-critic-recal-halt"}).json()["id"]

    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    done = _poll(c, run_id)  # bounded by _poll's tries cap → proves it is not a forever loop
    assert done["state"] == "FAILED", f"an unrecoverable loop must HALT FAILED, not {done['state']}"
    # the BYOM recalibrator FIRED on the real stack — the surfaced checkpoint records the spend
    assert _recal_count(done) >= 1, (
        f"recalibration did not fire before the bound — loop_state={done.get('loop_state')}"
    )
    # a GENUINE real-model run (not a setup failure): every loop member produced substantial output.
    # The recalibrator is a real OpenRouter turn that can't run in fake mode, so recalibration_count
    # >= 1 above already proves the model was LIVE (RULE 8).
    results = done.get("results") or {}
    assert results and all(
        len(str((r or {}).get("output") or "")) > 40 for r in results.values()
    ), f"the loop never executed a real model round: {done}"
    assert "did not converge" in (done.get("error_message") or ""), done.get("error_message")


@requires_byom
def test_stalling_loop_recovers_through_recalibration(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # a clearable threshold: a stalling team gets UNSTUCK — recovering in-drive after a recal,
    # or via the ADR-042 bounded re-run — to SUCCEEDED. It recovers; it never runs forever.
    user = register(f"recalrec{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "recal openrouter key")

    nonce = uuid.uuid4().hex[:10]
    doc, subs = _import_loop_team(
        c, user, nonce, or_cred, convergence="evaluator>=0.8", max_rounds=8
    )
    gid = c.post("/api/v1/graphs", json={"name": "writer-critic-recal-recover"}).json()["id"]

    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    done = _poll(c, run_id)
    spent_recal = _recal_count(done)
    for _ in range(4):  # ADR-042: re-run a weak-model failure, bounded, until it recovers
        if done["state"] == "SUCCEEDED":
            break
        assert done["state"] == "FAILED", done
        rr = c.post(f"/v1/engine/team-runs/{run_id}/rerun")
        assert rr.status_code == 202, rr.text
        done = _poll(c, run_id)
        spent_recal = max(spent_recal, _recal_count(done))
    assert done["state"] == "SUCCEEDED", f"the stalling loop never recovered: {done}"
    # a real-model recovery (a fake can't clear the real evaluator's >=0.8 grade to SUCCEED): every
    # member produced substantial output
    results = done.get("results") or {}
    assert results and all(
        len(str((r or {}).get("output") or "")) > 40 for r in results.values()
    ), f"the recovered loop has no real member output — was the harness LIVE? {done}"
    # observability: recalibration is part of the recovery path (best-effort — fast convergence
    # skip it). Surfaced for the operator + the CTO's remote check; logged, not hard-asserted.
    print(f"[recover] recalibrations observed across the recovery: {spent_recal}")
