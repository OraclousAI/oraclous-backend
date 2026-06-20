"""BYOM real-LLM agent run END-TO-END through the API GATEWAY — the user brings their own model.

This is the truest zero-fake test: a real user, through the gateway, **stores their own model token
via the real credential API** (`POST /credentials/`), then submits an agent whose OHM model
references that credential (`config.credential_id`) — and the **live** harness resolves it via the
broker and makes a **real OpenRouter call**. Nothing is injected server-side; the test only performs
the user's own API actions, and the model key comes from the env (the user's key), never hardcoded.

A random per-run nonce is echoed back: only a real LLM following the prompt can produce it (a
scripted / fake-mode responder cannot), so a pass proves the call was genuinely live.

Requires:
  - the harness in LIVE mode  (HARNESS_LLM_MODE=live — see scripts/e2e.sh --byom)
  - OPENROUTER_API_KEY in the env (the user's BYOM key)
Skipped otherwise, so it never reddens the deterministic suite or unit CI.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")  # the user's own key, provided via env
requires_byom_key = pytest.mark.skipif(
    not _USER_MODEL_KEY, reason="OPENROUTER_API_KEY not set (the user's BYOM model key)"
)


@requires_byom_key
def test_a_user_brings_their_own_model_token_and_runs_a_real_agent(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("BYOM User")
    c = gateway_client(user["token"])

    # 1) the user stores THEIR OWN model token via the real credential API (never server-side)
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
    credential_id = cred.json()["id"]

    # 2) the user submits an agent whose model references THEIR credential — a real OpenRouter call.
    #    The nonce is unguessable per run, so echoing it back can only come from a real LLM.
    nonce = uuid.uuid4().hex[:10]
    manifest = {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "BYOM Echo",
            "owner_organization_id": str(uuid.uuid4()),
        },
        "models": [
            {
                "role": "primary",
                "binding": "openrouter/openai/gpt-4o-mini",
                "protocol_shape": "openai-compatible",
                "config": {"credential_id": credential_id},  # the USER's stored credential
            }
        ],
        "actors": [{"role": "primary", "kind": "agent"}],
        "prompts": [
            {
                "role": "primary",
                "source": "inline",
                "body": f"Reply with exactly this token and nothing else: {nonce}",
            }
        ],
        "runtime": {"entrypoint": "primary"},
    }
    run = c.post("/v1/harnesses/execute", json={"manifest": manifest, "input": "Echo the token."})
    assert run.status_code in (200, 201), run.text
    body = run.json()
    assert body["status"] == "SUCCEEDED", body
    # only a real LLM that read the prompt produces the per-run nonce (fake/scripted mode cannot)
    assert nonce in str(body.get("output") or ""), (
        f"nonce {nonce!r} not echoed — is the harness in LIVE mode? output={body.get('output')!r}"
    )
