"""#907 — a run driven by the scripted stand-in model is LABELLED as such through the gateway.

The keystone proof for #907: register -> import a minimal studio -> run it THROUGH THE GATEWAY
(`:8006`) exactly as the two symptom reports did -> the settled run's read AND its status poll both
say ``simulated: true``. Deliberately NOT ``byom``-marked: the keyless CI suite (`scripts/e2e.sh`,
no ``OPENROUTER_API_KEY``) brings the stack up with ``HARNESS_LLM_MODE=fake`` (`scripts/e2e.sh:92`)
— it IS the fake stack, so this is the one e2e leg where the assertion is guaranteed meaningful
rather than a no-op. The BYOM leg's mirror assertion (``simulated is False`` on a real-LLM run)
lives in ``test_team_byom_real_llm_gateway_e2e.py``.

No fakes on the CLIENT side: real registration -> real JWT -> real engine -> real worker -> real
harness. The harness's OWN LLM client is the scripted stand-in ONLY because the deployed stack was
started with ``HARNESS_LLM_MODE=fake`` (the CI/dev default) — that is the condition under test, not
a test-side substitution.

Bring the stack up first (one line):
    HARNESS_LLM_MODE=fake docker compose -f deploy/docker-compose.yml \
        -f deploy/docker-compose.dev-ports.yml up -d
Then: uv run pytest tests/e2e -m e2e   (auto-skips when the gateway is unreachable)
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _reasoning_only_studio(root: Path) -> None:
    """ONE reasoning-only member, no tools, no gate — the smallest studio that SUCCEEDS on the fake
    LLM (which answers "No tools were available; nothing to do." on its first turn when the member
    declares no tools) without needing a real registry dispatch to resolve."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text(
        "---\nname: researcher\nmodel: sonnet\n---\nSummarise the topic in one sentence.\n"
    )
    (root / "teams" / "1-research").mkdir(parents=True)
    (root / "teams" / "1-research" / "charter.md").write_text(
        "# Team I — Research\n## Roster\n"
        "| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `researcher` | subagent | sonnet | research |\n"
    )


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 15) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


def test_a_fake_llm_run_is_labelled_simulated_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    _reasoning_only_studio(tmp_path)
    imported = import_setup(
        tmp_path, owner_organization_id=uuid.uuid4(), name="studio", substrate="file"
    )
    assert imported.manifest is not None
    body = {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {},
    }

    c = gateway_client(register(f"simulateduser{uuid.uuid4().hex[:10]} user")["token"])
    created = c.post("/v1/engine/team-runs", json=body)
    assert created.status_code == 202, created.text  # the worker drives it; request didn't block
    run_id = created.json()["id"]

    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED", done
    # #907: the stack under CI/dev runs HARNESS_LLM_MODE=fake — the settled run must say so.
    assert done["simulated"] is True

    status = c.get(f"/v1/engine/team-runs/{run_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["simulated"] is True  # the read and the status poll agree
