"""Scheduled ADOPTED-TOOL run fired AUTONOMOUSLY by Celery BEAT — end-to-end through the gateway.

#501-#8/#9: the #489 e2e (test_scheduled_adopted_tool_gateway_e2e.py) proves the FIRE-NOW shortcut.
This proves the genuinely-autonomous path — the Celery **Beat** tick (`engine.fire_schedules` every
minute) drives ``fire_due`` → ``_fire_adopted_tool`` → the registry-execute worker → the stamped
run, with NO ``fire-now`` call anywhere. That exercises the Beat→worker→registry HTTP wiring + the
broker that fire-now bypasses (the DEPLOYED-STACK VERIFICATION LAW: CI-green never covers this).

And the idempotency assertion is STRADDLE-SAFE (#9): instead of "fire twice, assert 1 run" (which
double-counts if the two fires cross a minute boundary), it observes a SINGLE minute window from
just past its boundary to ~50s in — both reads are inside one window, so a window's Beat fire is
counted at most once by construction. The stamped run is fetched as a REAL registry Execution.

No fakes, no mocks, no internal port, no DB-direct: registry + engine + worker + beat + broker are
real. The seeded **Send to Drafts** curated sink needs no credentials, so no BYOM key is required.

Bring the stack up first:
    docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev-ports.yml up -d
Then: uv run pytest tests/e2e -m e2e   (auto-skips when the gateway is unreachable)
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _sink_cap(c: httpx.Client) -> dict:
    """The seeded Send-to-Drafts curated sink (no credentials required → execute-ready)."""
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Send to Drafts" in by_name, f"send-to-drafts not seeded; got {sorted(by_name)}"
    return by_name["Send to Drafts"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "beat-scheduled-sink", "configuration": {}},
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _register_adopted_schedule(c: httpx.Client, instance_id: str) -> dict:
    resp = c.post(
        "/v1/engine/schedules",
        json={
            "type": "cron",
            "cron": "* * * * *",  # every minute → Beat fires it autonomously on the next tick
            "target_kind": "adopted_tool_run",
            "instance_id": instance_id,
            "input": "beat scheduled",
            "input_data": {
                "channel": "email",
                "content": "beat weekly digest",
                "recipient": "a@x.test",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_kind"] == "adopted_tool_run"
    assert body["last_fired_at"] is None  # a fresh schedule has NOT fired — Beat must do it
    return body


def _stamped_runs(c: httpx.Client, schedule_id: str) -> list[dict]:
    rows = c.get(f"/v1/engine/schedules/{schedule_id}/runs").json()["runs"]
    return [r for r in rows if r["execution_id"] is not None]


def test_beat_autonomously_fires_an_adopted_tool_schedule_exactly_once_per_window(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    # unique first token in the display name — avoid any auth org-slug collision across e2e runs.
    user = register(f"beatsched-{uuid.uuid4().hex[:8]} Owner")
    c = gateway_client(user["token"])

    cap = _sink_cap(c)
    iid = _instantiate(c, cap["id"])
    sched = _register_adopted_schedule(c, iid)
    sched_id = sched["id"]

    # ── #8: prove BEAT fires it autonomously — NO fire-now is ever called in this test. Beat ticks
    #    every 60s; allow up to ~2 windows for the tick + registry dispatch + the execution_id stamp
    stamped: list[dict] = []
    for _ in range(75):  # ~150s ceiling
        stamped = _stamped_runs(c, sched_id)
        if stamped:
            break
        time.sleep(2)
    # THE #8 proof: this test issued NO fire-now, yet the schedule produced a stamped run — the only
    # other way an adopted_tool_run schedule fires is the Celery Beat tick, so Beat drove the full
    # fire_due → _fire_adopted_tool → worker → registry chain autonomously on the deployed stack.
    assert stamped, "Beat never autonomously fired the adopted-tool schedule (no stamped run)"

    # the run is a REAL registry Execution (gateway-only), and the sink drafts (never sends).
    ex = c.get(f"/api/v1/executions/{stamped[0]['execution_id']}")
    assert ex.status_code == 200, ex.text
    body = ex.json()
    assert body["status"] in {"SUCCESS", "SUCCEEDED", "COMPLETED"}, body
    assert body["output_data"]["status"] == "DRAFT"

    # ── #9: STRADDLE-SAFE single-window idempotency. Align to just past a fresh minute boundary,
    #    then observe the SAME window from ~+2s to ~+50s — both reads inside one minute, so Beat's
    #    fire of THIS window is counted at most once (no boundary double-count). A double-fire in a
    #    window would show as a +2 jump; the dedupe (org, schedule:window) row forbids it.
    time.sleep(60 - datetime.now(UTC).second + 2)  # ~2s into a fresh minute window
    c0 = len(_stamped_runs(c, sched_id))
    time.sleep(48)  # still inside the SAME minute window (second ~50)
    c1 = len(_stamped_runs(c, sched_id))
    assert c1 - c0 <= 1, (
        f"Beat double-fired a single window: {c0} → {c1} stamped runs within one minute"
    )
