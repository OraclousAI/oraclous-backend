"""#1072 harness cancel-path DEPLOYED-STACK proof through the API GATEWAY — real-LLM (BYOM).

The user mints an ``execution_id`` client-side, starts a real ``POST /v1/harnesses/execute`` whose
task needs TEN sequential tool/LLM round-trips (deterministically outlasting both the client's own
short timeout and the time it takes cancel polling to land — a fast three-step task risked settling
SUCCEEDED before cancel ever reached it), waits a bounded delay for a real model turn to book
tokens, then cancels it with ``POST /v1/harnesses/{execution_id}/cancel``, polling every ~0.5s
rather than waiting on the client's own give-up timeout. Design (#1072, backend-implementer
ruling):

  * ``execute`` accepts an optional caller-supplied ``execution_id`` so a client can cancel a run
    before any response ever arrives.
  * ``cancel`` returns 200 with the terminal execution body (``status="CANCELLED"``, the REAL
    ``total_tokens`` spent before the loop stopped) once the loop has actually torn down, or 202
    ``{"execution_id": ..., "status": "CANCEL_REQUESTED"}`` while it is still tearing down.
  * a re-read 20s later must show the SAME status and the SAME ``total_tokens`` — proof the loop
    really stopped rather than continuing to spend in the background.
  * an unknown id, or another organisation's id, gets an identical 404 — the cancel flag is never
    set across a tenant boundary. Checked WHILE the run is still in flight (not after it settles),
    and the owner's own cancel afterwards still lands CANCELLED — the other org's attempt had no
    effect.

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

#: A short client-side timeout on the BACKGROUND thread's own request only — realism (a dropped
#: client connection, e.g. a closed browser tab, must not keep charging the user). Cancel timing
#: below never waits on THIS timeout — it runs on its own bounded schedule (see
#: _MIN_WORK_BOOKED_WAIT_SECONDS below).
_CLIENT_GIVES_UP_AFTER = 8.0
#: Cancel is polled frequently and independently of the client timeout above.
_CANCEL_POLL_INTERVAL = 0.5
#: Bounded wait after firing execute, BEFORE the first cancel attempt, so at least one real LLM
#: turn has landed and booked tokens before cancel can land. Cancel starting the instant execute
#: fires usually beats the first model reply; the design (#1072) says tokens from a call cut
#: mid-flight are never reported, so `total_tokens > 0` would flake for a timing reason, not a
#: product one. There is no gateway-visible progress signal to poll instead of a fixed delay:
#: `GET /v1/harnesses/executions/{execution_id}` 404s for the whole run — the execution row is
#: written once, at the end, when the loop finishes (see `harness_execution_service.execute`),
#: never at dispatch — so it cannot tell us a turn has landed. Kept well under the ten-step task's
#: total wall time so the run is still in flight when we use it below.
_MIN_WORK_BOOKED_WAIT_SECONDS = 6.0
#: A 404 is read as "the run hasn't started yet" only inside this window from the first poll; past
#: it, a persistent 404 is a real failure (unknown id / no cancel route), not a startup race.
_NOT_YET_STARTED_WINDOW_SECONDS = 15.0
#: Overall bound on 202 (CANCEL_REQUESTED, still tearing down) -> settled — generous: cancellation
#: is a Postgres lease flag (`cancel_requested_at`) that the owning replica's watcher polls
#: (~1s) and, on seeing it, cancels the loop's asyncio task — interrupting an in-flight LLM call,
#: rather than waiting for the loop to notice between iterations. Still generous because tearing
#: down (closing the LLM client, persisting the CANCELLED row, emitting provenance) after ten
#: real, sequential tool/LLM round-trips can legitimately take a while.
_CANCEL_SETTLE_TIMEOUT_SECONDS = 120.0
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


#: Ten fixed, self-contained math-tools calls (no step depends on a prior result, so a model never
#: needs to read a tool's output to keep going) cycling through all five curated operations twice.
#: Deliberately MANY steps, not three: three real sequential round-trips with a fast/cheap model
#: can finish close to or under the client's own give-up window, which would let the run settle
#: SUCCEEDED before cancel ever lands — flaky for the wrong reason. Ten forces enough wall time
#: that the cancel call (which never waits on the client's own give-up timeout) reliably lands
#: mid-run, well after _MIN_WORK_BOOKED_WAIT_SECONDS but well before the task could finish.
_MATH_STEPS = [
    "compound_growth with start=1000, rate=0.01, periods=1",
    "percentage_change with start=1000, end=1010",
    "break_even_units with fixed_costs=10000, price_per_unit=50, variable_cost_per_unit=30",
    "payback_period with initial_investment=50000, cash_flow_per_period=10000",
    'ratio with numerator=100, denominator=4, numerator_unit="USD", denominator_unit="unit"',
    "compound_growth with start=2000, rate=0.02, periods=2",
    "percentage_change with start=2000, end=2200",
    "break_even_units with fixed_costs=20000, price_per_unit=60, variable_cost_per_unit=35",
    "payback_period with initial_investment=80000, cash_flow_per_period=16000",
    'ratio with numerator=200, denominator=8, numerator_unit="USD", denominator_unit="unit"',
]


def _multi_step_manifest(org: str, credential_id: str) -> dict:
    """A harness manifest whose task needs MANY sequential tool round-trips (the seeded, keyless
    ``Math Tools`` group, #822) — one LLM turn to plan, then a tool call and an LLM turn per step,
    ten steps, so real network + generation latency across the whole loop reliably outlasts the
    time it takes an independently-polling cancel call to land, without depending on a second BYOM
    provider (Tavily etc)."""
    steps = "\n".join(f"{i}. Call operation {step}." for i, step in enumerate(_MATH_STEPS, 1))
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
                    "You have a tool group named math-tools. Complete these ten steps IN ORDER, "
                    "calling exactly ONE math-tools operation per turn and waiting for its result "
                    "before calling the next one — never call more than one tool in the same "
                    f"turn:\n{steps}\n"
                    "Only after all ten calls have returned, reply with one sentence naming the "
                    "results."
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
                        "input": "Run the ten math-tools steps and summarise.",
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


def _drop_request_id(body: dict) -> dict:
    """The gateway's own-error envelope (``{"error": {..., "requestId": ...}}``) mints a fresh
    ``requestId`` per response — strip it before comparing two error bodies for equality."""
    error = dict(body.get("error", {}))
    error.pop("requestId", None)
    return {**body, "error": error}


def _cancel_until_settled(c: httpx.Client, execution_id: uuid.UUID) -> httpx.Response:
    """Poll POST .../cancel every ``_CANCEL_POLL_INTERVAL`` — independent of the background
    request's own client timeout (callers wait out ``_MIN_WORK_BOOKED_WAIT_SECONDS`` before the
    first call, so real work is already booked; this loop only paces the calls after that). A 404
    is tolerated only inside ``_NOT_YET_STARTED_WINDOW_SECONDS`` (the execution row/lease may not
    exist yet); a 202 (CANCEL_REQUESTED, still tearing down) is polled until settled or
    ``_CANCEL_SETTLE_TIMEOUT_SECONDS`` runs out. Mirrors the route's documented 200/202/404
    contract (#1072 design)."""
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        resp = c.post(f"/v1/harnesses/{execution_id}/cancel")
        if resp.status_code == 404:
            assert elapsed < _NOT_YET_STARTED_WINDOW_SECONDS, (
                f"cancel still 404 after {elapsed:.1f}s — the execution never started (or the "
                f"cancel route / execution_id wiring doesn't exist yet): {resp.text}"
            )
            time.sleep(_CANCEL_POLL_INTERVAL)
            continue
        if resp.status_code == 202:
            assert elapsed < _CANCEL_SETTLE_TIMEOUT_SECONDS, (
                f"cancel still 202 CANCEL_REQUESTED after {elapsed:.1f}s — never settled: "
                f"{resp.text}"
            )
            time.sleep(_CANCEL_POLL_INTERVAL)
            continue
        return resp


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

    # 1) start a real run whose task needs ten sequential tool/LLM iterations. The background
    #    client gives up after ~8s (realism — a closed tab must not keep charging the user), but
    #    cancel polling below does NOT wait on that timeout.
    _fire_execute_in_background(gateway_url, user["token"], execution_id, manifest)

    # 1b) wait until real work is booked (see _MIN_WORK_BOOKED_WAIT_SECONDS) before touching cancel
    #    at all — otherwise cancel routinely lands before any LLM reply, and an interrupted call's
    #    tokens are never reported, making the total_tokens > 0 check below flake on timing rather
    #    than proving anything about the product. The ten-step task is still well in flight here.
    time.sleep(_MIN_WORK_BOOKED_WAIT_SECONDS)

    # 2) a second organisation cancelling the SAME execution_id, WHILE it is still in flight, gets
    #    a 404 identical (module requestId) to a 404 for a freshly minted unknown id from that same
    #    org/user — the cancel flag is never set across a tenant boundary (ADR-006), and org B gets
    #    no signal distinguishing "exists but not yours" from "doesn't exist".
    other = register(f"cancelother{uuid.uuid4().hex[:10]} user")
    other_c = gateway_client(other["token"])
    cross_org = other_c.post(f"/v1/harnesses/{execution_id}/cancel")
    assert cross_org.status_code == 404, cross_org.text
    unknown = other_c.post(f"/v1/harnesses/{uuid.uuid4()}/cancel")
    assert unknown.status_code == 404, unknown.text
    assert _drop_request_id(cross_org.json()) == _drop_request_id(unknown.json()), (
        cross_org.json(),
        unknown.json(),
    )

    # 3) cancel it (as the OWNER) — THE PROOF. A settled 200 must carry the CANCELLED status and
    #    the real, nonzero spend the loop had already made before it stopped. If the ten-step task
    #    still finished (SUCCEEDED/FAILED) before cancel ever landed, that is a timing defect in
    #    the test itself (not the feature) — fail loudly and distinctly from a real product
    #    assertion. A CANCELLED (not some already-cancelled variant) also proves org B's attempt
    #    above had no effect on this run.
    cancelled = _cancel_until_settled(c, execution_id)
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "CANCELLED", (
        f"the run settled as {body.get('status')!r} before cancel could land "
        f"(iterations={body.get('iterations')}, total_tokens={body.get('total_tokens')}) — "
        "the ten-step task finished too fast; this is a test-timing issue, not proof the cancel "
        f"path works. body={body}"
    )
    assert body["total_tokens"] > 0, body  # a real LLM turn happened before the cancel landed

    # 4) nothing kept spending after CANCELLED: a re-read well after settling shows the SAME row.
    time.sleep(_SETTLE_WAIT_SECONDS)
    reread = c.get(f"/v1/harnesses/executions/{execution_id}")
    assert reread.status_code == 200, reread.text
    settled = reread.json()
    assert settled["status"] == "CANCELLED", settled
    assert settled["total_tokens"] == body["total_tokens"], (
        f"total_tokens changed after CANCELLED ({body['total_tokens']} -> "
        f"{settled['total_tokens']}) — the loop kept running/spending after the cancel settled"
    )

    # 5) the confirmed spend is reflected on the org's own spend read, through the gateway — an
    #    org must be able to see what a cancelled run really cost, not just SUCCEEDED ones.
    spend = c.get("/v1/harnesses/spend")
    assert spend.status_code == 200, spend.text
    spend_body = spend.json()
    assert spend_body["total_input_tokens"] + spend_body["total_output_tokens"] > 0, spend_body
