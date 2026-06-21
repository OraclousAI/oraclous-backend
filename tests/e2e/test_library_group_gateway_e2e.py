"""Library-group DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#488 / ADR-038 D1).

A real user, through the gateway (:8006), discovers the seeded **Text Tools** library tool,
instantiates it, and dispatches each curated operation — which the registry runs in-process and
whose dict output lands on the org-scoped Execution row (readable through the gateway). An unknown
operation fails closed. Real capability-registry; nothing mocked, no internal port, no DB-direct
(rule 5). The package auto-skips when the gateway is down (conftest) — a skip is not a pass.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _text_tools_cap(c: httpx.Client) -> dict:
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Text Tools" in by_name, f"text-tools not seeded; got {sorted(by_name)}"
    return by_name["Text Tools"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "text-tools", "configuration": {}, "settings": {}},
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _run(c: httpx.Client, iid: str, payload: dict) -> dict:
    ex = c.post(f"/api/v1/instances/{iid}/execute", json={"input_data": payload})
    assert ex.status_code == 201, ex.text
    return ex.json()


def test_curated_library_operations_run_and_land_on_the_execution_row(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: each curated op dispatches in-process; its output persists on the org row."""
    user = register("Library Group")
    c = gateway_client(user["token"])
    cap = _text_tools_cap(c)
    iid = _instantiate(c, cap["id"])

    wc = _run(c, iid, {"operation": "word_count", "text": "the quick brown fox"})
    assert wc["status"] == "SUCCESS" and wc["output_data"]["count"] == 4

    up = _run(c, iid, {"operation": "to_upper", "text": "hello"})
    assert up["status"] == "SUCCESS" and up["output_data"]["result"] == "HELLO"

    em = _run(c, iid, {"operation": "extract_emails", "text": "a@x.test and b@y.test, a@x.test"})
    assert em["status"] == "SUCCESS" and em["output_data"]["emails"] == ["a@x.test", "b@y.test"]

    # the output persisted on the org-scoped Execution row, read back THROUGH THE GATEWAY
    got = c.get(f"/api/v1/executions/{wc['id']}")
    assert got.status_code == 200 and got.json()["output_data"]["count"] == 4


def test_unknown_operation_fails_closed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("Library Unknown")
    c = gateway_client(user["token"])
    cap = _text_tools_cap(c)
    iid = _instantiate(c, cap["id"])
    out = _run(c, iid, {"operation": "rm_rf", "text": "x"})
    assert out["status"] == "FAILED" and out["error_type"] == "INVALID_OPERATION"
