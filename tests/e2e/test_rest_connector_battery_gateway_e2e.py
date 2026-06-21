"""REST-connector DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#489 / ADR-039 D1).

A real user, through the gateway (:8006), discovers the seeded **REST Connector**, instantiates it
(keyless), and dispatches a curated source — which the registry fetches LIVE over HTTPS and whose
parsed dict lands on the org-scoped Execution row (read back through the gateway). The shipped
sources are public keyless GETs (mempool.space tip, alternative.me Fear & Greed), so the proof
needs no BYOM setup. An unknown source fails closed. Real registry + real outbound HTTPS; nothing
mocked, no internal port, no DB-direct (rule 5). The package auto-skips when the gateway is down
(a skip is not a pass).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _rest_connector_cap(c: httpx.Client) -> dict:
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "REST Connector" in by_name, f"rest-connector not seeded; got {sorted(by_name)}"
    return by_name["REST Connector"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": cap_id,
            "name": "rest-connector",
            "configuration": {},
            "settings": {},
        },
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _run(c: httpx.Client, iid: str, payload: dict) -> dict:
    ex = c.post(f"/api/v1/instances/{iid}/execute", json={"input_data": payload})
    assert ex.status_code == 201, ex.text
    return ex.json()


def test_curated_sources_fetch_live_and_land_on_the_execution_row(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: a keyless curated source is fetched LIVE over HTTPS through the gateway."""
    user = register("REST Connector")
    c = gateway_client(user["token"])
    cap = _rest_connector_cap(c)
    iid = _instantiate(c, cap["id"])

    tip = _run(c, iid, {"source_id": "mempool", "endpoint": "tip_height"})
    assert tip["status"] == "SUCCESS", tip
    height = tip["output_data"]["block_height"]
    assert isinstance(height, int) and height > 800_000  # a live Bitcoin chain tip

    fng = _run(c, iid, {"source_id": "alternative_me", "endpoint": "fear_greed"})
    assert fng["status"] == "SUCCESS", fng
    assert 0 <= fng["output_data"]["value"] <= 100 and fng["output_data"]["classification"]

    # the live value persisted on the org-scoped Execution row, read back THROUGH THE GATEWAY
    got = c.get(f"/api/v1/executions/{tip['id']}")
    assert got.status_code == 200 and got.json()["output_data"]["block_height"] == height


def test_unknown_source_fails_closed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("REST Unknown")
    c = gateway_client(user["token"])
    cap = _rest_connector_cap(c)
    iid = _instantiate(c, cap["id"])
    out = _run(c, iid, {"source_id": "evil-internal-source", "endpoint": "x"})
    assert out["status"] == "FAILED" and out["error_type"] == "UNKNOWN_SOURCE"
