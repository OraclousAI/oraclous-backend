"""Library-group DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#488 / ADR-038 D1).

A real user, through the gateway (:8006), discovers the seeded **Text Tools** library tool,
instantiates it, and dispatches each curated operation — which the registry runs in-process and
whose dict output lands on the org-scoped Execution row (readable through the gateway). An unknown
operation fails closed. Real capability-registry; nothing mocked, no internal port, no DB-direct
(rule 5). The package auto-skips when the gateway is down (conftest) — a skip is not a pass.
"""

from __future__ import annotations

import uuid
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


def _refused(c: httpx.Client, iid: str, payload: dict) -> dict:
    """An operation the instance's descriptor does not declare — refused BEFORE any executor.

    #1004 moved this refusal one step earlier and one service down. It used to be a 201 carrying a
    FAILED execution row (the connector's own whitelist answering `INVALID_OPERATION` after the
    executor had already been created); the registry now checks the requested operation against
    `spec.capabilities` and answers a coded 409 with no execution row at all.

    What a real user sees is the GATEWAY's view of that refusal, which is not the registry's. The
    proxy drains every upstream error body (it may carry internals) and re-mints the canonical
    envelope, relaying an upstream `error_code` only for the three allow-listed taxonomy values
    (`validation_passthrough._RELAYABLE_CODES`). `unsupported_operation` is a registry-local token,
    not a taxonomy value, so through `:8006` this arrives as a plain `CONFLICT` — the registry's
    token and the refused operation's NAME both stop at the wall. The harness is unaffected: it
    calls the registry directly, so it still gets the token and turns it into words for its member.
    """
    ex = c.post(f"/api/v1/instances/{iid}/execute", json={"input_data": payload})
    assert ex.status_code == 409, ex.text
    body = ex.json()
    assert body["error"]["code"] == "CONFLICT", ex.text
    return body


def test_curated_library_operations_run_and_land_on_the_execution_row(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: each curated op dispatches in-process; its output persists on the org row."""
    user = register(f"librarygroup{uuid.uuid4().hex[:10]} user")
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
    user = register(f"libraryunknown{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    cap = _text_tools_cap(c)
    iid = _instantiate(c, cap["id"])
    out = _refused(c, iid, {"operation": "rm_rf", "text": "x"})
    # Fail-closed all the way to the user: nothing the caller wrote comes back. The registry's own
    # message DOES name `rm_rf` (#692 actionability), but the gateway's wall drops it with the rest
    # of the upstream body — the known cost of moving this refusal to a 409, recorded on #1007.
    # Pinned so a later change that starts relaying it has to be a deliberate one.
    assert "rm_rf" not in str(out)

    # …and fail-closed means no execution happened at all: the refusal is not a FAILED run, so the
    # instance's counters never moved. This is the property the old 201 + FAILED row could not have.
    inst = c.get(f"/api/v1/instances/{iid}")
    assert inst.status_code == 200, inst.text
    assert inst.json()["execution_count"] == 0


def test_oversized_text_is_capped_and_an_adversarial_input_is_fast(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """#488 ReDoS hardening, proven through the gateway: the arg cap fail-closes an oversized text,
    and an adversarial-but-under-cap input returns promptly (the regex no longer backtracks)."""
    user = register(f"libraryredos{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    cap = _text_tools_cap(c)
    iid = _instantiate(c, cap["id"])

    oversized = _run(c, iid, {"operation": "to_upper", "text": "a" * 100_001})
    assert oversized["status"] == "FAILED" and oversized["error_type"] == "INVALID_INPUT"

    # what was minutes of CPU on the old quadratic regex now returns immediately
    adversarial = _run(c, iid, {"operation": "extract_emails", "text": "a@" + "." * 90_000})
    assert adversarial["status"] == "SUCCESS" and adversarial["output_data"]["emails"] == []
