"""File-native team-run BYOM real-LLM E2E through the GATEWAY — the item-8 §22 proof (#518, E6).

The binding acceptance for the file-native blackboard: a real team, given its own working tree once
at the run (``workspace_root``), has a member WRITE ``bible/*.md`` IN PLACE in that tree. The fake
harness calls the first tool with empty args, so it cannot produce a meaningful ``Write(path,
content)`` — only a LIVE model genuinely deciding to call Write can. So this is a real-LLM proof:
real registration → real JWT → real engine → real worker → real **live** harness → real OpenRouter
call → real ``Write`` tool → a file on the real filesystem under the org-scoped workspace. It is
then read back THROUGH THE GATEWAY to prove it landed in ``workspace_root`` (not default scratch).

No fakes, no DB-direct. Requires the harness LIVE + ``OPENROUTER_API_KEY`` (``scripts/e2e.sh
--byom``); skipped otherwise so it never reddens the deterministic suite or unit CI.

RED until #518 [impl] threads ``workspace_root`` → each member's file-tool instance config.
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

# The capability-registry container's default org-scoped workspaces root (#517 WORKSPACES_ROOT).
_WORKSPACES_ROOT = "/tmp/oraclous-agent-workspaces"  # noqa: S108 — container-local default
_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom_key = pytest.mark.skipif(
    _USER_MODEL_KEY is None, reason="OPENROUTER_API_KEY not set (BYOM real-LLM run)"
)


def _scribe_studio(root: Path, nonce: str) -> None:
    """A one-member file-native team whose member is told to WRITE the canon to bible/canon.md."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "scribe.md").write_text(
        "---\nname: scribe\nmodel: sonnet\ntools: Write, Read\n---\n"
        "You are the bible-keeper. Use the Write tool to create the file `bible/canon.md` "
        f"containing EXACTLY this text and nothing else: {nonce}\n"
        "Use no other tools. After writing, reply with the single word: done\n"
    )
    (root / "teams" / "1-canon").mkdir(parents=True)
    (root / "teams" / "1-canon" / "charter.md").write_text(
        "# Team I — Canon\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `scribe` | subagent | sonnet | write the canon |\n"
    )


def _byom_model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _cap_id(c: httpx.Client, name: str) -> str:
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    assert name in caps, f"{name} not seeded; got {sorted(caps)}"
    return caps[name]["id"]


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 40) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


@requires_byom_key
def test_a_file_native_member_writes_bible_in_place_through_a_team_run(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register("File-Native Team")
    c = gateway_client(user["token"])

    # the user stores THEIR OWN model key (never server-side)
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

    # the team's real working tree — under the org-scoped workspaces root (#517 confinement)
    work_tree = f"{_WORKSPACES_ROOT}/{user['org_id']}/book-{uuid.uuid4().hex[:8]}"
    nonce = f"canon-{uuid.uuid4().hex[:10]}"

    _scribe_studio(tmp_path, nonce)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": sub_harnesses,
            "gate_decisions": {},
            "workspace_root": work_tree,
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED", done

    # Prove the file landed IN the working tree (read it back through the gateway, working_dir-set).
    reader = c.post(
        "/api/v1/instances",
        json={
            "capability_id": _cap_id(c, "Read"),
            "name": f"verify-read-{uuid.uuid4().hex[:6]}",
            "configuration": {"working_dir": work_tree},
        },
    )
    assert reader.status_code == 201, reader.text
    out = c.post(
        f"/api/v1/instances/{reader.json()['id']}/execute",
        json={"input_data": {"operation": "read", "path": "bible/canon.md"}},
    ).json()
    assert out["status"] == "SUCCESS", out
    assert nonce in out["output_data"]["content"], out  # the live member wrote the canon in place
