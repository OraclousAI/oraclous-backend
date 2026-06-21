"""O1 secret onboarding END-TO-END through the API GATEWAY on the DEPLOYED stack — NO fakes (#490).

The acceptance for #490 (ADR-039): *"the first SCHEDULED run consumes secrets with no auth-prompt
wall."* A real user, through the application-gateway (`:8006`) with a real JWT:

POSITIVE (the headline): pastes a Tavily key ONCE via the credentials API (BYOM — source is
``TAVILY_API_KEY`` in deploy/.env, NEVER a registry server env), binds it to a keyed Web-Research
instance, registers a CRON ``adopted_tool_run`` schedule that runs a live ``search``, and fires it
via ``POST /v1/engine/schedules/{id}/fire-now``. The SCHEDULED run — engine → worker → registry
``execute_sync`` — resolves the stored per-org key from the broker **with no prompt** and returns
REAL web hits. The key never leaks. This is the whole O1 chain (store once → resolvable everywhere →
a scheduled run silently consumes it), reusing #486 (keyed web-research) + #500 (the scheduler).

NEGATIVE (the wall is a clean signal, not a dead end): an UNCONFIGURED keyed instance, executed
synchronously (the path that returns the body verbatim), fails closed with a typed, leak-safe
``needs_credential`` token — requirement_id + provider only, NEVER a secret or credential id
(#483) — so the user knows EXACTLY which key to paste, then re-runs.

Real registry + engine + worker + broker + a real Tavily call — nothing mocked, no internal port, no
DB-direct (FUCK_CLAUDE_FUCK_PAPERCLIP rule 5). The package auto-skips when the gateway is down; the
positive skips when the BYOM source is unset — a skip is NOT a pass (rule 3).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_TAVILY = os.environ.get("TAVILY_API_KEY", "")
_needs_key = pytest.mark.skipif(
    not _TAVILY, reason="TAVILY_API_KEY (BYOM source) not set — a skip is not a pass"
)


def _web_research_cap(c: httpx.Client) -> dict:
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Web Research" in by_name, f"web-research not seeded; got {sorted(by_name)}"
    return by_name["Web Research"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "o1-web-research", "configuration": {}},
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _store_key_once(c: httpx.Client, cap_id: str, user_id: str, iid: str) -> str:
    """Paste the BYOM key ONCE → per-org envelope (ADR-020) → bind to the instance. The store
    response must never echo the secret."""
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": cap_id,
            "user_id": user_id,
            "name": "my tavily key",
            "provider": "tavily",
            "cred_type": "api_key",
            "credential": {"api_key": _TAVILY},
        },
    )
    assert cred.status_code == 201, cred.text
    assert _TAVILY not in cred.text, "the BYOM secret must never be echoed by the store response"
    cid = cred.json()["id"]
    cfg = c.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"api_key": cid}},
    )
    assert cfg.status_code == 200, cfg.text
    return cid


def _register_search_schedule(c: httpx.Client, instance_id: str) -> str:
    resp = c.post(
        "/v1/engine/schedules",
        json={
            "type": "cron",
            "cron": "* * * * *",
            "target_kind": "adopted_tool_run",
            "instance_id": instance_id,
            "input": "scheduled-search",
            "input_data": {"operation": "search", "query": "Eurail global pass benefits"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_kind"] == "adopted_tool_run" and body["last_fired_at"] is None
    return body["id"]


def _poll_runs(c: httpx.Client, schedule_id: str, want: int, tries: int = 20) -> list[dict]:
    rows: list[dict] = []
    for _ in range(tries):
        rows = c.get(f"/v1/engine/schedules/{schedule_id}/runs").json()["runs"]
        stamped = [r for r in rows if r["execution_id"] is not None]
        if len(stamped) >= want:
            return stamped
        time.sleep(2)
    raise AssertionError(f"schedule {schedule_id} never reached {want} stamped runs (got {rows})")


@_needs_key
def test_a_scheduled_run_consumes_a_stored_secret_with_no_prompt(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE O1 PROOF: paste a key once → a SCHEDULED keyed run resolves the per-org secret with no
    auth-prompt wall and returns REAL web hits. The secret never leaves the broker."""
    user = register("O1 Onboarder")
    c = gateway_client(user["token"])

    # 1-4. discover + instantiate the keyed web-research tool, then paste the key ONCE and bind it.
    cap = _web_research_cap(c)
    iid = _instantiate(c, cap["id"])
    _store_key_once(c, cap["id"], user["user_id"], iid)

    # 5. schedule a live search against the keyed instance and fire it WITHOUT a Beat tick.
    sched_id = _register_search_schedule(c, iid)
    fired = c.post(f"/v1/engine/schedules/{sched_id}/fire-now")
    assert fired.status_code == 202, fired.text

    # 6. the SCHEDULED run (engine → worker → registry execute_sync) resolved the stored per-org key
    #    from the broker with NO prompt and ran a real search — read it gateway-only.
    stamped = _poll_runs(c, sched_id, want=1)
    exec_id = stamped[0]["execution_id"]
    ex = c.get(f"/api/v1/executions/{exec_id}")
    assert ex.status_code == 200, ex.text
    out = ex.json()
    assert out["status"] in {"SUCCESS", "SUCCEEDED", "COMPLETED"}, out  # no auth-prompt wall
    hits = out["output_data"]["hits"]
    assert isinstance(hits, list) and len(hits) >= 1, out  # a real Tavily call returned web results
    assert all(h["url"].startswith("http") for h in hits), hits
    assert _TAVILY not in ex.text  # the stored secret never surfaces in the run record


def test_an_unconfigured_keyed_tool_fails_closed_at_the_edge_with_no_leak(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The 'wall' fails closed: a missing required key returns a clean 409 through the gateway —
    no dispatch, no secret, no upstream internals. The registry emits a typed, leak-safe
    ``needs_credential`` token at the substrate (unit-proven in the capability-registry); surfacing
    that token through the gateway's ORA-37 envelope to the frontend onboarding prompt is a
    cross-repo Contract follow-up — the edge deliberately strips upstream error detail today
    (#490; Interface Contracts §3 rule 8)."""
    user = register("O1 NeedsCred")
    c = gateway_client(user["token"])
    cap = _web_research_cap(c)
    iid = _instantiate(c, cap["id"])  # NB: no key stored / bound

    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "search", "query": "anything"}},
    )
    assert ex.status_code == 409, ex.text  # fail-closed: required credential unmapped, no dispatch
    body = ex.json()
    assert body["error"]["code"] == "CONFLICT", body  # the canonical edge envelope
    assert body["error"]["retryable"] is False, body  # re-running without onboarding won't help
    assert "hits" not in ex.text  # the tool never dispatched
    assert "tvly-" not in ex.text.lower()  # no key material anywhere in the edge response
