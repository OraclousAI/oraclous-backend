"""Team-run END-TO-END through the API GATEWAY on the DEPLOYED docker stack — NO fakes.

This is the real user path, per the DEPLOYED-STACK VERIFICATION LAW (FUCK_CLAUDE_FUCK_PAPERCLIP.md /
CLAUDE.md §9): everything is driven through the **application-gateway** (`:8006`) with a **real JWT
from a real registration** — the gateway verifies the token and forwards to the real engine, which
enqueues a real Celery worker that drives the member DAG through the real harness-runtime. No
`FakeHarness`, no fake repos, no internal-function calls, no DB-direct assertions. The only
client-side step is the importer (the OHM library producing the request body, as a client does).

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


def _book_studio(root: Path) -> None:
    """A book-shaped studio: researcher -> [Gate A blocks] -> writer, reasoning-only so it runs on
    the deployed harness without E5 tool resolution, + a declared Hierarchy of Truth."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text(
        "---\nname: researcher\nmodel: sonnet\n---\nResearch the topic and propose an outline.\n"
    )
    (agents / "writer.md").write_text(
        "---\nname: writer\nmodel: sonnet\n---\nDraft the chapter from the approved outline.\n"
    )
    (root / "teams" / "1-research").mkdir(parents=True)
    (root / "teams" / "1-research" / "charter.md").write_text(
        "# Team I — Research\n## Roster\n"
        "| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `researcher` | subagent | sonnet | research |\n"
        "## Hard gates\n- **Gate A** — the author approves the outline before drafting.\n"
    )
    (root / "teams" / "2-write").mkdir(parents=True)
    (root / "teams" / "2-write" / "charter.md").write_text(
        "# Team II — Write\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `writer` | subagent | sonnet | draft |\n"
    )
    (root / "AGENTS.md").write_text(
        "## Hierarchy of Truth\n```\nrules/\n  >  bible/\n  >  outline/TOC.md\n  >  drafts/\n```\n"
    )


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 15) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


def test_book_studio_runs_through_the_gateway_with_a_blocking_gate(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Import a book studio, run it THROUGH THE GATEWAY: it pauses at the human gate (item 4b),
    advancing crosses it, and it completes — real auth, real worker, real harness, no fakes."""
    _book_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    body = {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {},
    }
    # precedence (item 9) was captured by the importer from the source's Hierarchy of Truth
    assert imported.report.precedence == ["rules", "bible", "outline/TOC.md", "drafts"]

    c = gateway_client(register("Studio Owner")["token"])
    created = c.post("/v1/engine/team-runs", json=body)
    assert created.status_code == 202, created.text  # the worker drives it; request didn't block
    run_id = created.json()["id"]

    paused = _poll(c, run_id, {"PAUSED", "SUCCEEDED", "FAILED"})
    assert paused["state"] == "PAUSED"  # item 4b — the run blocks at the human gate
    assert paused["paused_at"] == ["gate-a"]
    assert "writer" not in paused["results"]  # the writer is gated off

    adv = c.post(
        f"/v1/engine/team-runs/{run_id}/advance", json={"gate_decisions": {"gate-a": "approve"}}
    )
    assert adv.status_code == 202, adv.text
    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED"  # the writer ran only AFTER the gate was approved
    assert set(done["results"]) == {"researcher", "gate-a", "writer"}


def test_capability_ceiling_rejects_a_smuggled_send_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Item 4: a sub-harness that smuggles a 'send' capability past the writer's ceiling is rejected
    422 — at the gateway, before any run."""
    _book_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    subs = dict(imported.sub_harnesses)
    subs["writer"] = {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "writer",
            "owner_organization_id": str(uuid.uuid4()),
        },
        "capabilities": [{"ref": "core/send@1", "binding": "send"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }
    c = gateway_client(register("Ceiling Owner")["token"])
    resp = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": subs,
            "gate_decisions": {},
        },
    )
    assert resp.status_code == 422, resp.text  # the ceiling rejected the smuggled capability


def test_a_team_run_is_org_isolated_across_users_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Cross-tenant isolation through the gateway: user A's run is invisible to user B (RLS)."""
    _book_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    body = {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {},
    }
    run_id = (
        gateway_client(register("User A")["token"])
        .post("/v1/engine/team-runs", json=body)
        .json()["id"]
    )
    b = gateway_client(register("User B")["token"])
    assert b.get(f"/v1/engine/team-runs/{run_id}").status_code == 404  # B cannot see A's run


def test_run_tree_is_reachable_and_org_isolated_through_the_gateway(  # ADR-037 D3 / #471
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """The run-tree, end-to-end through the gateway on the deployed stack: a completed team run
    exposes its tree (root_execution_id == the run id + the member harness executions as children),
    and user B cannot read user A's tree (a cross-org tree id is a 404, never a leak — H1/H4)."""
    _book_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    # pre-approve the gate so the run drives straight to SUCCEEDED → a fully-populated tree
    body = {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {"gate-a": "approve"},
    }
    a = gateway_client(register("Tree Owner")["token"])
    created = a.post("/v1/engine/team-runs", json=body)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    done = _poll(a, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    assert done["state"] == "SUCCEEDED", done

    tree = a.get(f"/v1/engine/team-runs/{run_id}/tree")
    assert tree.status_code == 200, tree.text
    body_t = tree.json()
    assert body_t["team_run_id"] == run_id
    assert body_t["root_execution_id"] == run_id  # the run is its own tree root
    # both members (researcher + writer) ran as real harness executions → recorded as children
    assert len(body_t["child_execution_ids"]) >= 2

    b = gateway_client(register("Tree Intruder")["token"])
    assert b.get(f"/v1/engine/team-runs/{run_id}/tree").status_code == 404  # B can't read A's tree


def test_o4_status_surface_through_the_gateway(  # ADR-037 D5 / #472
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """The O4 light status, end-to-end through the gateway: a completed team run reports
    goal-attainment progress == 100 (not the old hardcoded 5/100), healthy, its accumulated token
    cost, and the terminal outcome; and user B cannot read user A's status (cross-org 404 — H3)."""
    _book_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    body = {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {"gate-a": "approve"},  # pre-approve → drives straight to SUCCEEDED
    }
    a = gateway_client(register("Status Owner")["token"])
    run_id = a.post("/v1/engine/team-runs", json=body).json()["id"]
    assert _poll(a, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})["state"] == "SUCCEEDED"

    status = a.get(f"/v1/engine/team-runs/{run_id}/status")
    assert status.status_code == 200, status.text
    s = status.json()
    assert s["team_run_id"] == run_id
    assert s["progress"] == 100  # goal-attainment by member completion (every member done)
    assert s["healthy"] is True and s["state"] == "SUCCEEDED" and s["last_outcome"] == "SUCCEEDED"
    assert (
        s["cost"]["tokens"] >= 0 and "usd" in s["cost"]
    )  # raw metering surfaced; usd priced later

    b = gateway_client(register("Status Intruder")["token"])
    assert b.get(f"/v1/engine/team-runs/{run_id}/status").status_code == 404  # B can't read A's


def test_flow_eval_gate_produces_a_verdict_without_branching_state(  # ADR-037 / #477
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """The flow-evaluation gate FIRES end-to-end through the gateway: a completed team run carrying
    a prose ``success_criteria`` is graded at the gate and a verdict is PRODUCED + STORED on the run
    (surfaced on the run read). The HARD E4/E8 boundary holds end-to-end — the run STATE is NOT
    branched on the verdict (it is SUCCEEDED regardless). The judge may be unconfigured on the
    stack, in which case the gate records a fail-closed verdict (pass=false) and the run SUCCEEDS —
    exactly the contract; a real PASS verdict is the EURail M2 milestone (#385)."""
    _book_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    manifest = imported.manifest.model_dump(mode="json")
    # the user declares a flow-evaluation gate on their team (a real OHM authoring choice). The
    # importer leaves orchestration null for a plain studio, so build the block.
    orchestration = manifest.get("orchestration") or {}
    orchestration["success_criteria"] = (
        "the chapter is well-structured, accurate, and faithful to the approved outline"
    )
    manifest["orchestration"] = orchestration
    body = {
        "manifest": manifest,
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {"gate-a": "approve"},  # pre-approve → drives straight to the gate
    }
    a = gateway_client(register("Verdict Owner")["token"])
    run_id = a.post("/v1/engine/team-runs", json=body).json()["id"]
    assert _poll(a, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})["state"] == "SUCCEEDED"

    run = a.get(f"/v1/engine/team-runs/{run_id}").json()
    assert run["state"] == "SUCCEEDED"  # the verdict NEVER branches the run state (E8 boundary)
    assert run["verdict"] is not None  # the gate fired and PRODUCED + STORED a verdict
    assert "pass" in run["verdict"]  # a typed verdict (pass/score/recommended_action)
