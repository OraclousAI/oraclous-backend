"""The validation desk reads a founder's idea back — END-TO-END through the API GATEWAY (#866).

A real user, registered through the gateway, pastes their OWN model key via the real credential
API, then asks the platform to read their idea back. A real model answers. Nothing is injected
server-side, nothing is mocked, and no service port is touched directly.

Three legs, and each one is a claim the desk depends on:

1. **The read.** An idea over the floor comes back as ordered spans marked ``read`` or
   ``inferred``, plus at most three questions. Liveness is proven by reading TWO unrelated ideas
   in the same run and checking each restatement reflects its own: a canned or fake-mode
   responder answers both the same way, and cannot describe bakery orders for one and translation
   invoices for the other. (An earlier version echoed a per-run invented word instead; a real
   reader summarises and legitimately drops such a word, so that assertion was flaky ~4% of runs
   while the endpoint itself was fine.)
2. **The instant refusal.** An idea under the floor is refused with ``IDEA_TOO_VAGUE``, and the
   refusal arrives fast enough that no model was called.
3. **The missing model.** With nothing connected, the call refuses with ``MODEL_NOT_CONNECTED``
   rather than borrowing a platform model. Both codes have to survive the gateway's error-body
   drain, which is the only reason they exist in the taxonomy at all.

Requires the harness in LIVE mode and OPENROUTER_API_KEY in the env (the user's own key).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom_key = pytest.mark.skipif(
    not _USER_MODEL_KEY, reason="OPENROUTER_API_KEY not set (the user's BYOM model key)"
)

_READBACK = "/v1/engine/intake/readback"
_COLLECT_BUDGET_SECONDS = 120.0


def _models(credential_id: str) -> list[dict]:
    return [
        {
            "role": "primary",
            "binding": "openrouter/openai/gpt-4o-mini",
            "protocol_shape": "openai-compatible",
            "config": {"credential_id": credential_id},
        }
    ]


def _collect(c: httpx.Client, run_id: str) -> httpx.Response:
    """Re-call with the run id until the reader settles, or the budget runs out.

    A 202 is the contract's answer to a slow model, not a failure — so the test follows it the way
    the screen will, rather than treating it as a pass on its own.
    """
    deadline = time.monotonic() + _COLLECT_BUDGET_SECONDS
    while True:
        resp = c.post(_READBACK, json={"readback_run_id": run_id}, timeout=60.0)
        if resp.status_code != 202:
            return resp
        assert time.monotonic() < deadline, "the reader never settled inside the collect budget"
        time.sleep(3)


_IDEA_A = (
    "I want to build an ordering tool for independent bakeries that still take their weekend "
    "orders on paper and lose track of about half of them."
)
_IDEA_B = (
    "I want to build an invoicing assistant for freelance translators who chase late payments "
    "by hand and cannot tell which agencies are the slow ones."
)


def _read(c: httpx.Client, idea: str, models: list[dict]) -> tuple[dict, float]:
    started = time.monotonic()
    resp = c.post(_READBACK, json={"idea": idea, "models": models}, timeout=60.0)
    if resp.status_code == 202:
        resp = _collect(c, resp.json()["readback_run_id"])
    assert resp.status_code == 200, resp.text
    return resp.json(), time.monotonic() - started


def _check_shape(body: dict) -> str:
    """Assert the contract and return the joined restatement."""
    spans = body["restatement"]
    assert isinstance(spans, list) and spans
    assert {s["source"] for s in spans} <= {"read", "inferred"}
    prose = "".join(s["text"] for s in spans)
    # joining the pieces has to give the screen a readable paragraph. An earlier prompt used an
    # angle-bracket placeholder in its example and the model copied it literally, wrapping every
    # piece in a tag — the restatement still "passed" every other check and was unreadable.
    assert "<" not in prose and ">" not in prose, prose
    questions = body["questions"]
    assert len(questions) <= 3
    for q in questions:
        assert q["id"] and q["text"]
        assert q["kind"] in ("text", "choice")
        assert (q["kind"] == "choice") == bool(q["options"])
    return prose


@requires_byom_key
def test_two_different_ideas_are_each_read_back_in_their_own_terms(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"deskuser{uuid.uuid4().hex[:8]} user")
    c = gateway_client(user["token"])

    # the user stores THEIR OWN model key through the real credential API
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
    models = _models(cred.json()["id"])

    body_a, elapsed_a = _read(c, _IDEA_A, models)
    body_b, elapsed_b = _read(c, _IDEA_B, models)
    prose_a = _check_shape(body_a).lower()
    prose_b = _check_shape(body_b).lower()

    # Each restatement is about ITS OWN idea. Nothing canned can satisfy both directions.
    assert "bak" in prose_a, prose_a
    assert "translat" in prose_b, prose_b
    assert "translat" not in prose_a, prose_a
    assert "bak" not in prose_b, prose_b
    # the questions are drawn from the idea too, not a fixed list reused for both
    assert [q["text"] for q in body_a["questions"]] != [q["text"] for q in body_b["questions"]]

    print(f"[#866] read-back settled in {elapsed_a:.1f}s and {elapsed_b:.1f}s")


@requires_byom_key
def test_an_idea_under_the_floor_is_refused_instantly_and_legibly(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"deskshort{uuid.uuid4().hex[:8]} user")
    c = gateway_client(user["token"])
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

    started = time.monotonic()
    resp = c.post(
        _READBACK,
        json={"idea": "a bakery app", "models": _models(cred.json()["id"])},
        timeout=30.0,
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "IDEA_TOO_VAGUE", resp.text
    # the floor is checked before any model call, so this cannot take a model round trip
    assert elapsed < 5.0, f"the refusal took {elapsed:.1f}s — a model was probably called"


def test_no_connected_model_refuses_rather_than_borrowing_one(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    # Deliberately NOT gated on a key: the point is that a founder with nothing connected is told
    # so, and the platform never quietly runs their idea through a model they did not choose.
    user = register(f"desknomodel{uuid.uuid4().hex[:8]} user")
    c = gateway_client(user["token"])
    idea = (
        "I want to build an ordering tool for independent bakeries that still take their weekend "
        "orders on paper and lose track of half of them."
    )
    resp = c.post(_READBACK, json={"idea": idea, "models": []}, timeout=30.0)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "MODEL_NOT_CONNECTED", resp.text
