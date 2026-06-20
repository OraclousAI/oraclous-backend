"""Team-run BYOM real-LLM END-TO-END through the API GATEWAY — the headline product surface, live.

The single-agent BYOM proof (`test_byom_real_llm_gateway_e2e.py`) showed one agent calling the
user's own model. This proves the same for a real **team of agents**: a user imports a multi-agent
studio, points every member's model at THEIR OWN stored credential, and runs it through
`POST /v1/engine/team-runs` — the engine enqueues a real Celery worker that drives the member DAG,
dispatching EACH member as a real `/v1/harnesses/execute` against the **live** harness, which
resolves the per-member credential via the broker and makes a **real OpenRouter call**.

No fakes: real registration → real JWT → real engine → real worker → real harness → real LLM. A
random per-run nonce is asked of every agent and must surface in the aggregated results — only
genuine LLM calls (not the fake-mode scripted responder) can produce it. The only client-side step
is the importer (the OHM library building the request body, as a client does) + overriding each
member's model to the user's BYOM model, exactly as a user choosing their own model would.

Requires (same as the single-agent BYOM test):
  - the harness in LIVE mode  (HARNESS_LLM_MODE=live — `scripts/e2e.sh --byom`)
  - OPENROUTER_API_KEY in the env (the user's BYOM key)
Skipped otherwise, so it never reddens the deterministic suite or unit CI.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")  # the user's own key, provided via env
requires_byom_key = pytest.mark.skipif(
    _USER_MODEL_KEY is None, reason="OPENROUTER_API_KEY not set (BYOM real-LLM run)"
)


def _echo_studio(root: Path, nonce: str) -> None:
    """A minimal two-member studio (researcher -> writer, NO blocking gate) where each agent is told
    to echo the per-run nonce — so a SUCCEEDED live run carries proof the LLM calls were real."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = f"Reply with exactly this token and nothing else: {nonce}"
    (agents / "researcher.md").write_text(f"---\nname: researcher\nmodel: sonnet\n---\n{body}\n")
    (agents / "writer.md").write_text(f"---\nname: writer\nmodel: sonnet\n---\n{body}\n")
    (root / "teams" / "1-research").mkdir(parents=True)
    (root / "teams" / "1-research" / "charter.md").write_text(
        "# Team I — Research\n## Roster\n| Agent | Type | Model | Job |\n"
        "| --- | --- | --- | --- |\n| `researcher` | subagent | sonnet | research |\n"
    )
    (root / "teams" / "2-write").mkdir(parents=True)
    (root / "teams" / "2-write" / "charter.md").write_text(
        "# Team II — Write\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `writer` | subagent | sonnet | draft |\n"
    )


def _byom_model(credential_id: str) -> dict:
    """The user's own model binding — a cheap OpenRouter model resolved via their credential."""
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 30) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


@requires_byom_key
def test_a_team_of_agents_runs_on_the_users_own_model_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register("Team BYOM User")
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

    # 2) import a real multi-agent studio, then point EVERY member's model at the user's credential
    nonce = uuid.uuid4().hex[:10]
    _echo_studio(tmp_path, nonce)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    assert set(sub_harnesses) == {"researcher", "writer"}
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]  # the user's BYOM model, per member

    # 3) run the team THROUGH THE GATEWAY — real engine -> worker -> live harness -> real OpenRouter
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": sub_harnesses,
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text  # the worker drives it; request didn't block
    run_id = created.json()["id"]

    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED", done  # every member's live harness call succeeded
    assert set(done["results"]) == {"researcher", "writer"}
    # only real LLM calls following the prompt produce the per-run nonce (fake mode cannot)
    assert nonce in str(done["results"]), (
        f"nonce {nonce!r} not in team results — is the harness LIVE? results={done['results']!r}"
    )
