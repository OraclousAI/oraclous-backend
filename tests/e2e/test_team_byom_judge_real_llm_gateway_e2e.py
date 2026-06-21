"""Team-run BYOM-JUDGE real-LLM END-TO-END through the API GATEWAY (ADR-037 / BYOM-judge).

The team-BYOM proof showed each *member* running on the user's own model. This proves the same for
the flow-evaluation JUDGE: the user stores ONE OpenRouter key via the gateway credentials API and
declares a `role="evaluator"` model on the team; `POST /v1/engine/team-runs` runs the team, and the
gate threads `judge_credential_id` to `core/evaluate`, which resolves THAT key per-org from the
credential-broker and grades the run with a **real OpenRouter call** — the verdict on the SUCCEEDED
row is a genuine `pass=true` from a real LLM, not the fail-closed `pass=false`.

NO server-env judge key: this is the whole point. The fail-closed verdict is ALWAYS `pass=false`, so
a `pass=true` here can only come from a real judge that resolved the user's key through the gateway
→ the broker. The deployed proof additionally runs KRS with `KRS_OPENAI_API_KEY` UNSET so the
operator singleton is absent and ONLY the BYOM path can produce a passing verdict.

Requires (`scripts/e2e.sh --byom`): the harness in LIVE mode + `OPENROUTER_API_KEY` set. Skipped
otherwise, so it never reddens the deterministic suite or unit CI.
"""

from __future__ import annotations

import json
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
    """A minimal two-member studio (researcher -> writer) where each agent echoes the per-run nonce,
    so the live run's output contains a token a real LLM judge can verifiably grade as present."""
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


def _byom_model(credential_id: str, role: str) -> dict:
    """The user's own model binding for ``role`` — a cheap OpenRouter model via their credential."""
    return {
        "role": role,
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
def test_team_run_is_judged_by_the_users_own_model_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register("Judge BYOM User")
    c = gateway_client(user["token"])

    # 1) the user stores THEIR OWN OpenRouter key via the real credential API (never server-side)
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "my openrouter judge key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _USER_MODEL_KEY},
        },
    )
    assert cred.status_code == 201, cred.text
    credential_id = cred.json()["id"]
    assert _USER_MODEL_KEY not in cred.text  # the secret is never echoed back

    # 2) import an echo studio; point each member's model AND a top-level evaluator model at the
    #    user's credential, with a success_criteria the echoed nonce verifiably satisfies
    nonce = uuid.uuid4().hex[:10]
    _echo_studio(tmp_path, nonce)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id, "primary")]  # live members on the user's model

    manifest = imported.manifest.model_dump(mode="json")
    manifest["models"] = [*(manifest.get("models") or []), _byom_model(credential_id, "evaluator")]
    orchestration = manifest.get("orchestration") or {}
    orchestration["success_criteria"] = f"The response contains the token {nonce}."
    manifest["orchestration"] = orchestration

    # 3) run the team THROUGH THE GATEWAY — real engine → worker → live harness, then the gate calls
    #    core/evaluate which resolves the user's key per-org from the broker and grades for real
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": manifest, "sub_harnesses": sub_harnesses, "gate_decisions": {}},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED", done
    # the members really ran on the live harness — only a genuine LLM call echoes the per-run nonce,
    # so the output the judge graded is REAL (not a fake-mode stub)
    assert nonce in str(done["results"]), f"nonce {nonce!r} not in results — is the harness LIVE?"

    # 4) a REAL PASS verdict — only a real LLM judge resolving the user's BYOM key can produce
    #    pass=true (the fail-closed path is ALWAYS pass=false); no server-env judge key was used
    run = c.get(f"/v1/engine/team-runs/{run_id}").json()
    verdict = run["verdict"]
    assert verdict is not None, "no verdict — the gate did not fire"
    assert verdict["pass"] is True, f"BYOM judge did not pass: {verdict}"
    assert float(verdict.get("score") or 0) >= 0.7
    assert "grader unavailable" not in json.dumps(verdict)  # NOT the fail-closed verdict
