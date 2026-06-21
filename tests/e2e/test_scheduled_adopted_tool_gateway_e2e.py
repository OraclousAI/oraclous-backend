"""Scheduled ADOPTED-TOOL run END-TO-END through the API GATEWAY on the DEPLOYED stack — NO fakes.

The real user path for #489 PR-3, per the DEPLOYED-STACK VERIFICATION LAW
(FUCK_CLAUDE_FUCK_PAPERCLIP.md / CLAUDE.md §9): everything is driven through the
**application-gateway** (`:8006`) with a **real JWT from a real registration**. A user discovers the
seeded **Send to Drafts** curated tool (proven executable by #489 PR-1/PR-2), instantiates it,
registers a CRON schedule whose
``target_kind == adopted_tool_run`` against that instance, and fires it WITHOUT a Beat tick via
``POST /v1/engine/schedules/{id}/fire-now``. The engine writes the (org, schedule:window)
idempotency row BEFORE enqueuing the registry dispatch, so the registry runs the instance once. The
proof is read gateway-only: the engine surfaces the schedule's adopted-tool runs (with the stamped
registry ``execution_id``), and each is fetched as a real registry Execution. Firing AGAIN in the
SAME window dispatches NOTHING — the dedupe row blocks the second registry execution.

No fakes, no mocks, no internal port, no DB-direct: registry + engine + worker + broker are real.

Bring the stack up first (one line):
    docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev-ports.yml up -d
Then: uv run pytest tests/e2e -m e2e   (auto-skips when the gateway is unreachable)
"""

from __future__ import annotations

import time
from collections.abc import Callable

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
        json={"capability_id": cap_id, "name": "scheduled-sink", "configuration": {}},
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _register_adopted_schedule(c: httpx.Client, instance_id: str) -> str:
    resp = c.post(
        "/v1/engine/schedules",
        json={
            "type": "cron",
            "cron": "* * * * *",
            "target_kind": "adopted_tool_run",
            "instance_id": instance_id,
            "input": "scheduled",
            "input_data": {
                "channel": "email",
                "content": "scheduled weekly digest",
                "recipient": "a@x.test",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_kind"] == "adopted_tool_run"
    assert body["instance_id"] == instance_id
    assert body["last_fired_at"] is None  # a fresh schedule has not fired
    return body["id"]


def _poll_runs(c: httpx.Client, schedule_id: str, want: int, tries: int = 15) -> list[dict]:
    """Poll the engine for the schedule's adopted-tool runs (gateway-only) until ``want`` rows carry
    a stamped registry execution_id (the worker dispatched + stamped them)."""
    rows: list[dict] = []
    for _ in range(tries):
        rows = c.get(f"/v1/engine/schedules/{schedule_id}/runs").json()["runs"]
        stamped = [r for r in rows if r["execution_id"] is not None]
        if len(stamped) >= want:
            return stamped
        time.sleep(2)
    raise AssertionError(f"schedule {schedule_id} never reached {want} stamped runs (got {rows})")


def test_a_scheduled_adopted_tool_fires_once_and_is_idempotent(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF + the merge gate: fire-now runs the curated instance exactly once; a second
    same-window fire-now produces NO second registry execution (the idempotency row blocked it)."""
    user = register("Sched Adopt")
    c = gateway_client(user["token"])

    # 1-2. discover + instantiate the curated send-to-drafts sink (execute-ready, no credentials).
    cap = _sink_cap(c)
    iid = _instantiate(c, cap["id"])

    # 3. register an adopted_tool_run schedule against the instance (fresh, last_fired_at=None).
    sched_id = _register_adopted_schedule(c, iid)

    # 4. fire it WITHOUT a Beat tick → 202; the cursor advances to the window.
    fired = c.post(f"/v1/engine/schedules/{sched_id}/fire-now")
    assert fired.status_code == 202, fired.text
    assert fired.json()["last_fired_at"] is not None  # window advanced

    # 5. prove the registry execution actually ran — read the engine's run rows (gateway-only) for
    #    the stamped registry execution_id, then fetch the REAL registry Execution by that id.
    stamped = _poll_runs(c, sched_id, want=1)
    assert len(stamped) == 1
    exec_id = stamped[0]["execution_id"]
    ex = c.get(f"/api/v1/executions/{exec_id}")
    assert ex.status_code == 200, ex.text
    ex_body = ex.json()
    assert ex_body["status"] in {"SUCCESS", "SUCCEEDED", "COMPLETED"}, ex_body
    assert ex_body["output_data"]["status"] == "DRAFT"  # the sink only drafts, never sends

    # 6. IDEMPOTENCY: fire AGAIN in the SAME window → 202, but NO second registry execution. The
    #    create-idempotent-before-dispatch row (org, schedule:window) blocked the second dispatch.
    again = c.post(f"/v1/engine/schedules/{sched_id}/fire-now")
    assert again.status_code == 202, again.text
    # give a putative (wrongly-fired) second dispatch ample time to surface, then assert it did NOT
    time.sleep(6)
    runs = c.get(f"/v1/engine/schedules/{sched_id}/runs").json()["runs"]
    assert len(runs) == 1, f"a second same-window fire double-fired: {runs}"
    assert runs[0]["execution_id"] == exec_id  # still the one-and-only execution


def test_a_scheduled_adopted_tool_run_is_org_isolated(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Cross-tenant isolation through the gateway: user B cannot see / fire user A's schedule."""
    a = gateway_client(register("Sched Owner A")["token"])
    cap = _sink_cap(a)
    iid = _instantiate(a, cap["id"])
    sched_id = _register_adopted_schedule(a, iid)

    b = gateway_client(register("Sched Intruder B")["token"])
    # B cannot fire A's schedule (a cross-org id is a 404, never a leak)
    assert b.post(f"/v1/engine/schedules/{sched_id}/fire-now").status_code == 404
    # and B sees none of A's runs
    assert b.get(f"/v1/engine/schedules/{sched_id}/runs").json()["runs"] == []
