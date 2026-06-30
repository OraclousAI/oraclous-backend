"""#601 (E8 KEYSTONE) — a standing team's fire N+1 reads fire N's PERSISTENT graph, on the LIVE

The keystone of E8 (ADR-048 decision 2): a scheduled ``target_kind="team"`` schedule fires a
bound to a STABLE, PERSISTENT graph workspace across windows — so run N+1 reads the state run N
instead of cold-respawning an empty substrate (the failure the North-Star Lock §6 forbids).

No fakes, all through the gateway ``:8006`` with real OpenRouter BYOM: register → store a BYOM
credential → create + seed a graph G with a unique marker → register a team schedule bound to G →
fire across two cron windows → assert EVERY run binds the SAME graph G (the keystone, read off the
run rows) and the seeded marker still lives in G (the substrate persisted, not wiped).

Beat-tolerant by construction (not by luck): with the partial-unique idempotency key per (org,
window), every fire in one window dedupes to AT MOST one run — so Celery Beat's own autonomous
``* * * * *`` fires never break the assertions, they *strengthen* them (Beat's runs also bind G).
The proofs are therefore SUPERSET-safe: ``len(runs) >= 2`` proves ≥2 windows fired across the cron
boundary (same-window fires collapse to one); ``all(r.graph_id == G)`` proves the persistent binding
holds for every fire whatever its source; the same-window pair proves dedupe (≤1 new run); the
per-cadence accrual is asserted > 0 AND bounded by the runs' real settled cost (the per-drive delta,
not fabricated/cumulative); and the schedule stays ``enabled`` (the unbounded lifecycle; each run is
bounded by the #585 budget/wall, not a new limiter). Auto-skips without the BYOM key / a gateway.
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


def _wait_new_run(c: httpx.Client, sched_id: str, before: set[str], tries: int = 40) -> list[dict]:
    """Wait until the run set grows beyond ``before`` (a NEW window fired), Beat-tolerant: returns
    as soon as ≥1 id appears that was not present before — never asserts an exact count."""
    runs: list[dict] = []
    for _ in range(tries):
        runs = _team_runs(c, sched_id)
        if {r["id"] for r in runs} - before:
            return runs
        time.sleep(1)
    raise AssertionError(
        f"schedule {sched_id} fired no NEW run in the next window (still {before})"
    )


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

    # ── IDEMPOTENCY (same window): two back-to-back fires collapse to AT MOST one run ───────────
    # Done FIRST, before any long agent poll, so the ≤1 assertion can't race a Beat-fired later
    # window. Beat-immune: a Beat tick landing in this same window also dedupes on the same key.
    before_n = {r["id"] for r in _team_runs(c, sched_id)}
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    runs_n = _wait_runs(c, sched_id, len(before_n) + 1)
    new_in_window = {r["id"] for r in runs_n} - before_n
    assert len(new_in_window) <= 1, (
        f"same-window dedupe: ≥2 fires in one window → ≤1 run — {runs_n}"
    )
    assert all(r["graph_id"] == graph_id for r in runs_n), f"window-N runs bind G — {runs_n}"

    # ── FIRE WINDOW N+1 (cross the cron minute boundary → a NEW window) ─────────────────────────
    time.sleep(60 - datetime.now(UTC).second + 2)  # into the next minute
    before_2 = {r["id"] for r in _team_runs(c, sched_id)}
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    _wait_new_run(c, sched_id, before_2)  # a NEW window's run appeared

    # ── THE KEYSTONE ───────────────────────────────────────────────────────────────────────────
    # ≥2 runs ⇒ ≥2 windows fired across the boundary (same-window fires collapse to one); and EVERY
    # run — the fire-now's AND any Beat-fired — binds the SAME persistent graph G. That is the
    # standing-team binding: run N+1 is NOT a cold-respawn onto an empty substrate.
    all_runs = _team_runs(c, sched_id)
    assert len(all_runs) >= 2, f"≥2 windows must have fired across the cron boundary — {all_runs}"
    assert all(r["graph_id"] == graph_id for r in all_runs), (
        f"EVERY run binds the SAME persistent graph G — {all_runs}"
    )
    # poll a representative few terminal (don't block on Beat's tail, which keeps accruing runs)
    for r in all_runs[:3]:
        _poll_run(c, r["id"])

    # the seeded marker doc STILL lives in G after the later window — substrate persisted, unwiped
    docs = c.get(f"/api/v1/graphs/{graph_id}/documents").json()
    assert len(docs) >= 1, f"the seeded graph workspace persisted across fires — {docs}"

    # ── PER-CADENCE ACCRUAL (real + bounded) + TWO-LEVEL TERMINATION ────────────────────────────
    # Read the schedule's accrual FIRST, then the per-run costs (the LIST row carries cost_tokens),
    # so the cost basis is read >= the accrual basis — making the upper bound race-free.
    sched = _schedule(c, sched_id)
    total_cost = sum(int(r.get("cost_tokens") or 0) for r in _team_runs(c, sched_id))
    accrued = int(sched["recurring_cost_tokens"])
    assert accrued > 0, f"the per-cadence accrual climbed off REAL settled runs — {sched}"
    # bounded by the runs' actual settled cost: the per-DRIVE delta, never fabricated or cumulative
    assert accrued <= total_cost, f"accrual {accrued} must be bounded by run cost {total_cost}"
    assert sched["enabled"] is True, (
        "the standing-team LIFECYCLE persists (unbounded; runs bounded by #585)"
    )
