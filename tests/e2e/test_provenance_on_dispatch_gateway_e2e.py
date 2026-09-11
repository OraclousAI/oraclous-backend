"""#826 DEPLOYED-STACK proof: the flagship runtime's provenance is visible through the gateway.

Two legs, both through the application-gateway (`:8006`) only — no fakes, no service ports, no
`/internal`, no DB-direct assertions (FUCK_CLAUDE_FUCK_PAPERCLIP.md rule 5):

1. A real team run's lifecycle (claim, dispatch, finish) lands on ``GET /v1/engine/activity`` —
   the headline gap the 24 August 2026 solution-architect ruling on #826 named: "our flagship
   runtime emits nothing and appears in neither /v1/engine/activity nor /v1/engine/usage". This
   leg needs a REAL model (the user's own OpenRouter key) — a scripted fake-mode run is never a DoD
   proof (rule 8) — so it carries ``byom``/``byom_smoke`` per tests/e2e/README.md.

2. A direct tool invocation (``POST /api/v1/instances/{id}/execute``, keyless — Math Tools) shows up
   on ``GET /api/v1/provenance`` — the capability-registry's own read route from the CTO's 11
   September 2026 ruling. That route (and its writing side, ``execute_sync``'s emit) is the OTHER
   #826 worker's slice (packages/substrate, capability-registry-service); this leg is the
   engine-side proof that the gateway actually fronts it end to end — noted as a cross-worker
   dependency in the test-author report, not something this test defines the shape of.

Bring the stack up first:
    scripts/e2e.sh --up            # the deterministic leg
    scripts/e2e.sh --byom-smoke    # harness LIVE, this file's byom_smoke test included
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

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.audit]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom_key = pytest.mark.skipif(
    _USER_MODEL_KEY is None, reason="OPENROUTER_API_KEY not set (#826 dispatch-provenance e2e)"
)


def _echo_studio(root: Path, nonce: str) -> None:
    """A minimal two-member studio (researcher -> writer), each told to echo a per-run nonce — the
    same shape test_team_byom_real_llm_gateway_e2e.py uses for its real-LLM proof."""
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
    return {
        "role": "primary",
        "binding": os.environ["E2E_MODEL"],
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
@pytest.mark.byom
@pytest.mark.byom_smoke  # #826: the team-run dispatch surface, on the real-model subset
def test_a_team_runs_lifecycle_is_visible_on_the_activity_feed_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"provdispatch{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    # 1) the user's OWN model key, through the real credential API — never injected server-side
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

    # 2) a real two-member team, each member pointed at the user's own credential
    nonce = uuid.uuid4().hex[:10]
    _echo_studio(tmp_path, nonce)
    imported = import_setup(
        tmp_path, owner_organization_id=uuid.uuid4(), name="studio", substrate="file"
    )
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]

    # 3) run it THROUGH THE GATEWAY: real engine -> real Celery worker -> live harness -> real LLM
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": sub_harnesses,
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED", done
    assert nonce in str(done["results"]), f"nonce {nonce!r} missing — is the harness LIVE?"

    # 4) THE PROOF: the run's lifecycle is on the activity feed, through the gateway, not the DB.
    activity = c.get("/v1/engine/activity", params={"limit": 200})
    assert activity.status_code == 200, activity.text
    events = activity.json()["events"]
    resource = f"engine_team_run:{run_id}"
    this_run = [e for e in events if e["resource"] == resource]
    assert this_run, f"no activity events at all for {resource} — the run emits nothing (#826)"

    actions = {e["action"] for e in this_run}
    assert "engine.team_run.start" in actions
    assert "engine.team_run.dispatch" in actions
    assert "engine.team_run.finish" in actions
    # every lifecycle record names WHO acted (the #826 11 Sep ruling's newly-exposed field)
    assert all(e.get("principal") for e in this_run), this_run
    # at least one record attests a real output (sha256:<64 hex> — never the raw payload, CLAUDE.md
    # §11). The exact hash is unknowable from outside the run, so only the shape is pinned here.
    hashed = [e for e in this_run if e.get("output_hash")]
    assert hashed, f"no lifecycle event carried an output_hash: {this_run}"
    output_hash = hashed[0]["output_hash"]
    assert output_hash.startswith("sha256:") and len(output_hash) == len("sha256:") + 64, (
        output_hash
    )


def test_a_direct_tool_invocation_is_visible_through_the_gateways_provenance_route(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """The direct-invoke leg (no model, no credential): the registry's OWN emit + read route (the
    other #826 worker's slice) is proven end to end through the SAME gateway a user actually hits.
    Math Tools is keyless and deterministic, matching test_math_tools_gateway_e2e.py's shape."""
    c = gateway_client(register(f"provdirectinv{uuid.uuid4().hex[:10]} user")["token"])

    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    assert "Math Tools" in caps, f"math-tools not seeded; got {sorted(caps)}"
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": caps["Math Tools"]["id"],
            "name": "math-tools-provenance",
            "configuration": {},
            "settings": {},
        },
    )
    assert inst.status_code == 201, inst.text
    instance_id = inst.json()["id"]

    ex = c.post(
        f"/api/v1/instances/{instance_id}/execute",
        json={"input_data": {"operation": "percentage_change", "start": 100, "end": 150}},
    )
    assert ex.status_code == 201, ex.text
    assert ex.json()["status"] == "SUCCESS", ex.json()

    # THE PROOF: the dispatch is readable back through the gateway's provenance route — never a
    # DB-direct assertion. This route does not exist yet (11 Sep CTO ruling, capability-registry
    # slice) — RED until the other #826 worker lands it.
    prov = c.get("/api/v1/provenance", params={"limit": 50})
    assert prov.status_code == 200, prov.text
    body = prov.json()
    assert body["total"] >= 1, body
    matching = [e for e in body["events"] if e["resource"] == f"tool_instance:{instance_id}"]
    assert matching, f"no provenance record for tool_instance:{instance_id}: {body['events']}"
