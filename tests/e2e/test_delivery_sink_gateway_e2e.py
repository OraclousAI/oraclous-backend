"""Delivery-sink DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#489 / ADR-039 D1).

A real user, through the gateway (:8006), discovers the seeded **Send to Drafts** sink, instantiates
it, and dispatches a delivery the registry records as a DRAFT on the org-scoped Execution row
(read back through the gateway). The sink structurally only drafts (status DRAFT, never SENT) and an
invalid channel fails closed. Real registry; nothing mocked, no internal port, no DB-direct.
The package auto-skips when the gateway is down (a skip is not a pass).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _sink_cap(c: httpx.Client) -> dict:
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Send to Drafts" in by_name, f"send-to-drafts not seeded; got {sorted(by_name)}"
    return by_name["Send to Drafts"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": cap_id,
            "name": "send-to-drafts",
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


def test_a_delivery_is_recorded_as_a_draft_on_the_org_row(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: the sink records a DRAFT (never sent) that persists on the org Execution row."""
    user = register("Delivery Sink")
    c = gateway_client(user["token"])
    cap = _sink_cap(c)
    iid = _instantiate(c, cap["id"])

    out = _run(
        c, iid, {"channel": "email", "content": "weekly digest draft", "recipient": "a@x.test"}
    )
    assert out["status"] == "SUCCESS", out
    assert out["output_data"]["status"] == "DRAFT"  # the sink only drafts, never sends
    assert out["output_data"]["channel"] == "email"
    assert out["output_data"]["content"] == "weekly digest draft"

    # the draft persisted on the org-scoped Execution row, read back THROUGH THE GATEWAY
    got = c.get(f"/api/v1/executions/{out['id']}")
    assert got.status_code == 200 and got.json()["output_data"]["status"] == "DRAFT"


def test_an_invalid_channel_fails_closed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("Delivery Invalid")
    c = gateway_client(user["token"])
    cap = _sink_cap(c)
    iid = _instantiate(c, cap["id"])
    out = _run(c, iid, {"channel": "telepathy", "content": "x"})
    assert out["status"] == "FAILED" and out["error_type"] == "INVALID_INPUT"
