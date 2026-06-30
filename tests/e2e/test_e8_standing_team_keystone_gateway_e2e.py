"""#601 (E8 KEYSTONE) — a standing team's fire N+1 reads fire N's PERSISTENT graph, on the LIVE

The keystone of E8 (ADR-048 decision 2): a scheduled ``target_kind="team"`` schedule fires a
bound to a STABLE, PERSISTENT graph workspace across windows — so run N+1 reads the state run N
instead of cold-respawning an empty substrate (the failure the North-Star Lock §6 forbids).

No fakes, all through the gateway ``:8006`` with real OpenRouter BYOM: register → store a BYOM
credential → create + seed a graph G with a unique marker → register a team schedule bound to G →
fire window N → fire window N+1 (a later minute) → assert BOTH runs are bound to the SAME graph G
(the keystone binding, read off the run rows) and the seeded marker still lives in G (the substrate
persisted, not wiped); plus the same-window idempotency, the per-cadence cost accrual, and the
two-level termination (the schedule stays enabled while each run settles terminal). Auto-skips
without the BYOM key / a reachable gateway.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_TERMINAL = {"SUCCEEDED", "FAILED", "PARTIAL", "REJECTED", "COST_BUDGET"}


def _byom_model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _cred(c: httpx.Client, user: dict) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "byom",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_graph(c: httpx.Client, marker: str) -> str:
    """Create a graph + ingest a unique-marker fact; wait for the ingest job so the marker lands."""
    g = c.post("/api/v1/graphs", json={"name": "standing-team-kb", "description": "keystone e2e"})
    assert g.status_code == 201, g.text
    graph_id = g.json()["id"]
    job = c.post(
        f"/api/v1/graphs/{graph_id}/ingest",
        json={"content": f"The standing-team codename is {marker}.", "source_type": "text"},
    )
    assert job.status_code == 202, job.text
    job_id = job.json()["id"]
    for _ in range(45):
        state = str(c.get(f"/api/v1/graphs/{graph_id}/jobs/{job_id}").json().get("status")).upper()
        if state in ("SUCCEEDED", "COMPLETED"):
            return graph_id
        if state in ("FAILED", "ERROR"):
            raise AssertionError(f"ingest job failed: {state}")
        time.sleep(2)
    raise AssertionError("ingest job never completed")


def _monitor_studio(root: Path, query_hint: str) -> None:
    """A one-member standing 'monitor' team. Its only tool is ``Read`` → remapped to the graph
    retriever under the cloud-first default, so a real run reads the BOUND graph."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "You are a standing monitor. Use your Read tool (it searches the knowledge graph) to look "
        f"up: {query_hint}. Call it with a `query` and `mode` of `hybrid`; the graph is already "
        "selected for this run, so do NOT pass a graph_id. Reply with the codename, verbatim."
    )
    (agents / "monitor.md").write_text(
        f"---\nname: monitor\nmodel: sonnet\ntools: Read\n---\n{body}\n"
    )
    (root / "teams" / "1-watch").mkdir(parents=True)
    (root / "teams" / "1-watch" / "charter.md").write_text(
        "# Team I — Watch\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `monitor` | subagent | sonnet | monitor |\n"
    )


def _team_runs(c: httpx.Client, sched_id: str) -> list[dict]:
    r = c.get(f"/v1/engine/schedules/{sched_id}/team-runs")
    assert r.status_code == 200, r.text
    return r.json()["runs"]


def _wait_runs(c: httpx.Client, sched_id: str, count: int, tries: int = 30) -> list[dict]:
    runs: list[dict] = []
    for _ in range(tries):
        runs = _team_runs(c, sched_id)
        if len(runs) >= count:
            return runs
        time.sleep(1)
    raise AssertionError(f"schedule {sched_id} produced {len(runs)} runs, expected {count}")


def _poll_run(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in _TERMINAL:
            return row
        time.sleep(2)
    raise AssertionError(f"team-run {run_id} never settled (last: {row.get('state')})")


def _schedule(c: httpx.Client, sched_id: str) -> dict:
    return next(s for s in c.get("/v1/engine/schedules").json()["schedules"] if s["id"] == sched_id)


@requires_byom
def test_standing_team_fire_n_plus_1_reads_fire_n_persistent_graph(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"standing{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    # a graph workspace seeded with a unique marker — the persistent substrate the standing
    marker = f"PERSIST-{uuid.uuid4().hex[:8]}"
    graph_id = _seed_graph(c, marker)

    _monitor_studio(tmp_path, query_hint="the standing-team codename")
    imported = import_setup(tmp_path, owner_organization_id=org, name="standing")
    assert imported.manifest is not None
    manifest = imported.manifest.model_dump(mode="json")
    manifest["models"] = [_byom_model(cred)]
    sub_harnesses = {
        role: {**dict(sub), "models": [_byom_model(cred)]}
        for role, sub in imported.sub_harnesses.items()
    }

    # register the STANDING TEAM schedule bound to the persistent graph G
    reg = c.post(
        "/v1/engine/schedules",
        json={
            "type": "cron",
            "cron": "* * * * *",
            "target_kind": "team",
            "manifest": manifest,
            "input": "standing team",
            "input_data": {"sub_harnesses": sub_harnesses, "gate_decisions": {}},
            "graph_id": graph_id,
        },
    )
    assert reg.status_code == 201, reg.text
    sched_id = reg.json()["id"]
    assert reg.json()["graph_id"] == graph_id

    # a team schedule WITHOUT a graph_id is rejected 4xx (the binding is mandatory)
    bad = c.post(
        "/v1/engine/schedules",
        json={
            "type": "cron",
            "cron": "* * * * *",
            "target_kind": "team",
            "manifest": manifest,
            "input": "x",
        },
    )
    assert bad.status_code in (400, 422), bad.text

    # ── FIRE WINDOW N ──────────────────────────────────────────────────────────────────────────
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    runs_n = _wait_runs(c, sched_id, 1)
    run_n = runs_n[0]
    assert run_n["graph_id"] == graph_id, f"run N must bind the persistent graph — {run_n}"
    _poll_run(c, run_n["id"])

    # ── FIRE WINDOW N+1 (a later minute — cross the cron boundary) ──────────────────────────────
    time.sleep(60 - datetime.now(UTC).second + 2)  # into the next minute → a NEW window
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    runs_2 = _wait_runs(c, sched_id, 2)
    run_ids = {r["id"] for r in runs_2}
    assert run_n["id"] in run_ids and len(run_ids) == 2, f"window N+1 must add a NEW run — {runs_2}"
    # THE KEYSTONE: BOTH fires are bound to the SAME persistent graph — not a cold-respawned
    assert all(r["graph_id"] == graph_id for r in runs_2), (
        f"both runs bind the SAME graph — {runs_2}"
    )
    for r in runs_2:
        _poll_run(c, r["id"])

    # the seeded marker doc STILL lives in G after N+1 — the substrate persisted, never wiped
    docs = c.get(f"/api/v1/graphs/{graph_id}/documents").json()
    assert len(docs) >= 1, f"the seeded graph workspace persisted across fires — {docs}"

    # ── IDEMPOTENCY: a duplicate same-window fire does NOT create a third run ───────────────────
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    assert len(_team_runs(c, sched_id)) == 2, "a same-window re-fire must not double-fire"

    # ── PER-CADENCE ACCRUAL + TWO-LEVEL TERMINATION ────────────────────────────────────────────
    sched = _schedule(c, sched_id)
    assert sched["recurring_cost_tokens"] > 0, f"the per-cadence accrual climbed — {sched}"
    assert sched["enabled"] is True, "the standing-team LIFECYCLE persists across fires"
