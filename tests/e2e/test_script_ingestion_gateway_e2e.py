"""Script-ingestion DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#487 / ADR-038 D1).

A real user, through the gateway (:8006), discovers the seeded **Script Ingestion** tool,
instantiates it, and dispatches a curated loader (``loader_id='synthetic'``) — which the registry
runs as a REAL guarded subprocess in-container and whose JSON output lands on the org-scoped
Execution row (readable back through the gateway). Negatives prove an unknown loader and a failing
loader fail closed without leaking stderr. Real capability-registry + a real subprocess; nothing
mocked, no internal port, no DB-direct (rule 5). The package auto-skips when the gateway is
down (conftest) — a skip is not a pass.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _script_ingestion_cap(c: httpx.Client) -> dict:
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Script Ingestion" in by_name, f"script-ingestion not seeded; got {sorted(by_name)}"
    return by_name["Script Ingestion"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": cap_id,
            "name": "script-ingestion",
            "configuration": {},
            "settings": {},
        },
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def test_curated_loader_runs_as_a_subprocess_and_output_lands_in_the_org_store(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: dispatch a curated loader → real subprocess → output on the org Execution row."""
    user = register("Script Ingest")
    c = gateway_client(user["token"])
    cap = _script_ingestion_cap(c)
    iid = _instantiate(c, cap["id"])

    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"loader_id": "synthetic", "args": {"count": 3}}},
    )
    assert ex.status_code == 201, ex.text
    out = ex.json()
    assert out["status"] == "SUCCESS", out
    records = out["output_data"]["records"]
    assert isinstance(records, list) and len(records) == 3, out
    assert out["output_data"]["loader_id"] == "synthetic" and out["output_data"]["exit_code"] == 0

    # the output persisted on the org-scoped Execution row, readable back THROUGH THE GATEWAY
    got = c.get(f"/api/v1/executions/{out['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["output_data"]["records"][0]["title"] == "synthetic-row-1"


def test_unknown_loader_fails_closed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("Script Unknown")
    c = gateway_client(user["token"])
    cap = _script_ingestion_cap(c)
    iid = _instantiate(c, cap["id"])
    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"loader_id": "definitely-not-a-loader"}},
    )
    assert ex.status_code == 201, ex.text
    assert ex.json()["status"] == "FAILED" and ex.json()["error_type"] == "INVALID_INPUT"


def test_failing_loader_is_loader_failed_and_never_leaks_stderr(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("Script Fail")
    c = gateway_client(user["token"])
    cap = _script_ingestion_cap(c)
    iid = _instantiate(c, cap["id"])
    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"loader_id": "synthetic-fail"}},
    )
    assert ex.status_code == 201, ex.text
    out = ex.json()
    assert out["status"] == "FAILED" and out["error_type"] == "LOADER_FAILED"
    assert "CANARY" not in ex.text and "/etc/passwd" not in ex.text  # stderr never echoed
