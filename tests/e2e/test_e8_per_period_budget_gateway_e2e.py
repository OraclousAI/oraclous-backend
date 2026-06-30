"""#598 (E8-L3) — a standing team's PER-PERIOD budget cap pauses the fleet, on the LIVE stack.

ADR-044 L3 / ADR-048 decision 4(b): a standing ``target_kind="team"`` schedule carries a user-set
period (daily|weekly|monthly) + per-period token allowance; spend ACCRUES across the scheduled fires
in the window and the fleet is PAUSED (the schedule disabled) when the window's allowance is hit —
never a silent overrun. Distinct from the per-RUN pool (#585, which resets every run): the cap reads
the per-CADENCE accrual the #601 keystone maintains across fires.

No fakes, all through the gateway ``:8006`` with real OpenRouter BYOM: register → store a BYOM
credential → seed a graph → register a team schedule bound to it with a TINY daily token allowance →
fire window N (a real BYOM run) → the run's REAL cost accrues into the day's window and crosses the
tiny cap → fire window N+1 (a later minute, same day) → assert the fleet is PAUSED
(``enabled=False`` + ``budget_paused=True``, surfaced) and the later fire did NOT execute (the
pre-flight skip), proving it is the WINDOW cap — not the per-run pool — that paused it.

The boundary RESET + resume cannot be wall-clock-waited on a live stack (a real day/week/month), so
it is proven deterministically in the unit suite (``test_schedule_service`` —
``test_team_window_rolled_resets_the_accrual_then_fires`` + the resume-sweep tests over a
controllable ``now``); this e2e proves the live accrual + cap-trip + pause + skip. Auto-skips
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
# << one real BYOM monitor run's token cost, so the FIRST settled run breaches the day's window.
_TINY_ALLOWANCE = 50


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


def _seed_graph(c: httpx.Client) -> str:
    g = c.post("/api/v1/graphs", json={"name": "budget-kb", "description": "L3 e2e"})
    assert g.status_code == 201, g.text
    graph_id = g.json()["id"]
    job = c.post(
        f"/api/v1/graphs/{graph_id}/ingest",
        json={"content": "The standing team watches the ledger.", "source_type": "text"},
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


def _monitor_studio(root: Path) -> None:
    """A one-member standing 'monitor' team whose only tool is Read (→ the graph retriever), so a
    real fire makes a real BYOM call that costs real tokens."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "You are a standing monitor. Use your Read tool (it searches the knowledge graph) to look "
        "up what the standing team watches. Call it with a `query` and `mode` of `hybrid`; the "
        "graph is already selected, so do NOT pass a graph_id. Reply with one short sentence."
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


def _schedule(c: httpx.Client, sched_id: str) -> dict:
    return next(s for s in c.get("/v1/engine/schedules").json()["schedules"] if s["id"] == sched_id)


def _poll_run(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in _TERMINAL:
            return row
        time.sleep(2)
    raise AssertionError(f"team-run {run_id} never settled (last: {row.get('state')})")


def _wait_accrual(c: httpx.Client, sched_id: str, at_least: int, tries: int = 60) -> int:
    """Poll the schedule's per-cadence accrual until it reaches ``at_least`` (the worker accrues
    each settled run's RAW cost at settle). Returns the accrual."""
    accrued = 0
    for _ in range(tries):
        accrued = int(_schedule(c, sched_id)["recurring_cost_tokens"])
        if accrued >= at_least:
            return accrued
        time.sleep(2)
    raise AssertionError(f"accrual stalled at {accrued}, expected >= {at_least}")


def _sleep_to_next_minute() -> None:
    time.sleep(60 - datetime.now(UTC).second + 1)  # just past a boundary → a fresh cron window


@requires_byom
def test_per_period_budget_pauses_the_standing_fleet_and_skips_the_next_fire(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"budget{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])
    graph_id = _seed_graph(c)

    _monitor_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=org, name="budget")
    assert imported.manifest is not None
    manifest = imported.manifest.model_dump(mode="json")
    manifest["models"] = [_byom_model(cred)]
    sub_harnesses = {
        role: {**dict(sub), "models": [_byom_model(cred)]}
        for role, sub in imported.sub_harnesses.items()
    }

    # register a STANDING TEAM with a TINY daily token allowance — one real run breaches the day.
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
            "budget_period": "daily",
            "budget_allowance_tokens": _TINY_ALLOWANCE,
        },
    )
    assert reg.status_code == 201, reg.text
    sched_id = reg.json()["id"]
    body = reg.json()
    assert body["budget_period"] == "daily" and body["budget_allowance_tokens"] == _TINY_ALLOWANCE
    assert body["budget_window_start"] is not None  # the window anchor is stamped at register
    assert body["enabled"] is True and body["budget_paused"] is False

    # a per-period cap on a non-team schedule is rejected (the cap is team-only)
    bad = c.post(
        "/v1/engine/schedules",
        json={
            "type": "cron",
            "cron": "* * * * *",
            "manifest_ref": "h",
            "input": "x",
            "budget_period": "daily",
            "budget_allowance_tokens": 100,
        },
    )
    assert bad.status_code in (400, 422), bad.text

    # ── FIRE WINDOW N: a real BYOM run whose real cost accrues into the day's window ──────────────
    _sleep_to_next_minute()
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    runs = _team_runs(c, sched_id)
    assert len(runs) >= 1, f"window N must have created a run — {runs}"
    _poll_run(c, runs[0]["id"])  # let the run settle so its cost accrues

    # the REAL run cost accrued into the schedule's per-cadence window total, past the tiny cap —
    # this is the cross-run accumulator, NOT a single run's pool.
    accrued = _wait_accrual(c, sched_id, _TINY_ALLOWANCE)
    assert accrued >= _TINY_ALLOWANCE > 0, f"real cost must accrue past the cap — {accrued}"

    # ── FIRE WINDOW N+1 (a later minute, SAME day): the pre-flight must PAUSE + SKIP ──────────────
    _sleep_to_next_minute()
    before = {r["id"] for r in _team_runs(c, sched_id)}
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    time.sleep(4)  # let the synchronous fire path run its pre-flight (it commits before returning)
    after = {r["id"] for r in _team_runs(c, sched_id)}
    assert after == before, (
        f"an exhausted window must NOT fire a new run (pre-flight skip) — {after - before}"
    )

    # the fleet is PAUSED — disabled BY the budget cap (surfaced, distinct from a manual disable)
    sched = _schedule(c, sched_id)
    assert sched["enabled"] is False, f"the standing fleet must be paused on breach — {sched}"
    assert sched["budget_paused"] is True, f"the pause reason is surfaced as L3 — {sched}"
    assert int(sched["recurring_cost_tokens"]) >= _TINY_ALLOWANCE  # the window stays over the cap
