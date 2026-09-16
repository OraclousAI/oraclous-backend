"""#1072 harness cancel-path DEPLOYED-STACK proof through the API GATEWAY — real-LLM (BYOM).

The user mints an ``execution_id`` client-side, starts a real ``POST /v1/harnesses/execute`` whose
task needs several sequential tool/LLM round-trips (so it outlives a short client-side timeout —
the browser tab closing must NOT keep spending the user's model tokens forever), then cancels it
with ``POST /v1/harnesses/{execution_id}/cancel``. Design (#1072, backend-implementer ruling):

  * ``execute`` accepts an optional caller-supplied ``execution_id`` so a client can cancel a run
    before any response ever arrives.
  * ``cancel`` returns 200 with the terminal execution body (``status="CANCELLED"``, the REAL
    ``total_tokens`` spent before the loop stopped) once the loop has actually torn down, or 202
    ``{"execution_id": ..., "status": "CANCEL_REQUESTED"}`` while it is still tearing down.
  * a re-read 20s later must show the SAME status and the SAME ``total_tokens`` — proof the loop
    really stopped rather than continuing to spend in the background.
  * an unknown id, or another organisation's id, gets an identical 404 — the cancel flag is never
    set across a tenant boundary.

None of this exists yet (no ``execution_id`` field, no cancel route) — every assertion below is
expected to fail RED against the current stack. Real registration -> real JWT -> the user's OWN
OpenRouter credential (BYOM, never injected server-side) -> the live harness -> a real OpenRouter
call; the multi-step task additionally drives the seeded, keyless ``Math Tools`` capability so the
run needs multiple tool round-trips, nothing mocked, no internal port, no DB-direct assertion
(FUCK_CLAUDE_FUCK_PAPERCLIP.md rule 5). ``byom``-marked -> deselected in CI; a fake-LLM run is
never a DoD proof (CLAUDE.md §9 rule 8). Auto-skips when the gateway is down (conftest); a skip is
not a pass.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")  # the user's own key, provided via env
requires_byom_key = pytest.mark.skipif(
    not _USER_MODEL_KEY, reason="OPENROUTER_API_KEY not set (the user's BYOM model key)"
)

#: A short client-side timeout: the request MUST still be running server-side when this expires
#: (the whole point of the feature — a dropped client connection must not keep charging the user).
_CLIENT_GIVES_UP_AFTER = 8.0
#: Bounded polling for the cancel call's own 202 (still tearing down) -> 200 (settled) transition.
_CANCEL_POLL_TRIES = 8
_CANCEL_POLL_SLEEP = 3.0
#: How long to wait before re-reading the execution, to prove nothing kept spending after CANCELLED.
_SETTLE_WAIT_SECONDS = 20.0


def _store_model_credential(c: httpx.Client, user: dict) -> str:
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "my openrouter model",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _USER_MODEL_KEY},
        },
    )
    assert cred.status_code == 201, cred.text
    return cred.json()["id"]


def _multi_step_manifest(org: str, credential_id: str) -> dict:
    """A harness manifest whose task needs several sequential tool round-trips (the seeded, keyless
    ``Math Tools`` group, #822): one LLM turn to plan, then a tool call and an LLM turn per step,
    three steps — enough real network + generation latency to reliably outlive an 8s client
    timeout, without depending on a second BYOM provider (Tavily etc)."""
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "cancel-path-math",
            "owner_organization_id": org,
        },
        "prompts": [
            {
                "role": "primary",
                "source": "inline",
                "body": (
                    "You have a tool group named math-tools. Complete these three steps IN ORDER, "
                    "calling exactly one math-tools operation per step and waiting for each result "
                    "before the next call:\n"
                    "1. Call operation compound_growth with start=1000, rate=0.02, periods=6.\n"
                    "2. Call operation percentage_change with start=1000, end=1200.\n"
                    "3. Call operation ratio with numerator=180, denominator=12, "
                    'numerator_unit="USD", denominator_unit="unit".\n'
                    "Only after all three calls have returned, reply with one sentence naming all "
                    "three numeric results."
                ),
            }
        ],
        "actors": [{"role": "primary", "kind": "agent"}],
        "models": [
            {
                "role": "primary",
                "binding": os.environ["E2E_MODEL"],
                "protocol_shape": "openai-compatible",
                "config": {"credential_id": credential_id},
            }
        ],
        "capabilities": [{"ref": "core/math-tools@1.0.0", "binding": "math-tools"}],
        "runtime": {"entrypoint": "primary"},
    }


def _fire_execute_in_background(
    gateway_url: str, token: str, execution_id: uuid.UUID, manifest: dict
) -> threading.Thread:
    """POST /v1/harnesses/execute with a short client-side timeout, in its own thread + its own
    client. The client giving up (ReadTimeout) must NOT stop the server-side run — that gap is
    exactly what the lease-backed cancel path exists to close."""

    def _run() -> None:
        try:
            with httpx.Client(
                base_url=gateway_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_CLIENT_GIVES_UP_AFTER,
            ) as short_client:
                short_client.post(
                    "/v1/harnesses/execute",
                    json={
                        "manifest": manifest,
                        "input": "Run the three math-tools steps and summarise.",
                        "execution_id": str(execution_id),
                    },
                )
        except httpx.TimeoutException:
            pass  # expected: the client gives up long before the loop is done
        except httpx.HTTPError:
            pass  # the server-side run is what the test verifies, not this thread's own outcome

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def _cancel_until_settled(c: httpx.Client, execution_id: uuid.UUID) -> httpx.Response:
    """Poll POST .../cancel through 202 (CANCEL_REQUESTED, still tearing down) until 200 (settled),
    bounded — mirrors the route's own documented 200/202/404 contract (#1072 design)."""
    last: httpx.Response | None = None
    for _ in range(_CANCEL_POLL_TRIES):
        last = c.post(f"/v1/harnesses/{execution_id}/cancel")
        if last.status_code != 202:
            return last
        time.sleep(_CANCEL_POLL_SLEEP)
    assert last is not None
    return last


@requires_byom_key
def test_a_cancelled_execution_stops_spending_and_is_org_scoped(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    gateway_url: str,
) -> None:
    user = register(f"cancelpath{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    credential_id = _store_model_credential(c, user)

    execution_id = uuid.uuid4()
    manifest = _multi_step_manifest(user["org_id"], credential_id)

    # 1) start a real run whose task needs several tool/LLM iterations, with a client that gives up
    #    (~8s) long before a three-step tool loop against a real model finishes.
    _fire_execute_in_background(gateway_url, user["token"], execution_id, manifest)

    # 2) cancel it — THE PROOF. A settled 200 must carry the CANCELLED status and the real,
    #    nonzero spend the loop had already made before it stopped.
    cancelled = _cancel_until_settled(c, execution_id)
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "CANCELLED", body
    assert body["total_tokens"] > 0, body  # a real LLM turn happened before the cancel landed

    # 3) nothing kept spending after CANCELLED: a re-read well after settling shows the SAME row.
    time.sleep(_SETTLE_WAIT_SECONDS)
    reread = c.get(f"/v1/harnesses/executions/{execution_id}")
    assert reread.status_code == 200, reread.text
    settled = reread.json()
    assert settled["status"] == "CANCELLED", settled
    assert settled["total_tokens"] == body["total_tokens"], (
        f"total_tokens changed after CANCELLED ({body['total_tokens']} -> "
        f"{settled['total_tokens']}) — the loop kept running/spending after the cancel settled"
    )

    # 4) the confirmed spend is reflected on the org's own spend read, through the gateway — an
    #    org must be able to see what a cancelled run really cost, not just SUCCEEDED ones.
    spend = c.get("/v1/harnesses/spend")
    assert spend.status_code == 200, spend.text
    spend_body = spend.json()
    assert spend_body["total_input_tokens"] + spend_body["total_output_tokens"] > 0, spend_body

    # 5) a second organisation cancelling the SAME execution_id gets an identical 404 — the cancel
    #    flag is never set across a tenant boundary (ADR-006).
    other = register(f"cancelother{uuid.uuid4().hex[:10]} user")
    other_c = gateway_client(other["token"])
    cross_org = other_c.post(f"/v1/harnesses/{execution_id}/cancel")
    assert cross_org.status_code == 404, cross_org.text
