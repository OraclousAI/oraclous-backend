"""Org-scoped team-run LIST END-TO-END through the API GATEWAY on the DEPLOYED stack — NO fakes.

The #633 read the FE Runs page (J6) + Approvals inbox (J7) consume, exercised exactly as the console
will: a real user registers, seeds real team runs through the gateway (`:8006`) via
`POST /v1/engine/team-runs` (real auth → real engine → real Celery worker → real harness), then
`GET /v1/engine/team-runs` lists them newest-first, `?state=PAUSED` returns only the run blocked at
a human gate, the page is paginated, an unknown state is a 422, and a second user sees NONE of the
first user's runs (RLS). Nothing mocked, no internal port, no DB-direct (FUCK_CLAUDE_FUCK_PAPERCLIP
rule 5).

Bring the stack up first: HARNESS_LLM_MODE=fake docker compose -f deploy/docker-compose.yml \
    -f deploy/docker-compose.dev-ports.yml up -d   (the suite auto-skips when the gateway is down).
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
    the deployed harness without tool resolution."""
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


def _body(tmp_path: Path, gate_decisions: dict) -> dict:
    _book_studio(tmp_path)
    imported = import_setup(
        tmp_path, owner_organization_id=uuid.uuid4(), name="studio", substrate="file"
    )
    assert imported.manifest is not None
    return {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": gate_decisions,
    }


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 20) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


def _seed_runs(c: httpx.Client, tmp_path: Path) -> tuple[str, str]:
    """Seed two runs in the caller's org: one PAUSED at the gate (no decision), one pre-approved →
    SUCCEEDED. Returns (paused_id, done_id)."""
    paused_id = c.post("/v1/engine/team-runs", json=_body(tmp_path / "p", {})).json()["id"]
    done_id = c.post(
        "/v1/engine/team-runs", json=_body(tmp_path / "d", {"gate-a": "approve"})
    ).json()["id"]
    assert _poll(c, paused_id, {"PAUSED", "SUCCEEDED", "FAILED"})["state"] == "PAUSED"
    assert _poll(c, done_id, {"SUCCEEDED", "FAILED", "REJECTED"})["state"] == "SUCCEEDED"
    return paused_id, done_id


def test_team_runs_are_listed_filtered_and_paginated_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    c = gateway_client(register(f"runsowner{uuid.uuid4().hex[:10]} user")["token"])
    paused_id, done_id = _seed_runs(c, tmp_path)

    # (1) the full list — both runs, newest-first, in the {team_runs, total} shape
    listed = c.get("/v1/engine/team-runs")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    ids = [r["id"] for r in body["team_runs"]]
    assert set(ids) >= {paused_id, done_id}
    assert body["total"] >= 2
    stamps = [r["created_at"] for r in body["team_runs"]]
    assert stamps == sorted(stamps, reverse=True)  # newest-first by created_at

    # (2) a list ROW is lean (a runs-table row, never the full readout) + carries the team name
    row = next(r for r in body["team_runs"] if r["id"] == paused_id)
    assert row["team_name"] == "studio"  # dug from manifest.metadata.name
    assert row["state"] == "PAUSED" and row["paused_at"] == ["gate-a"]
    for forbidden in ("manifest", "results", "sub_harnesses"):
        assert forbidden not in row, f"{forbidden} leaked into a list row"

    # (3) the state filter — ?state=PAUSED returns ONLY the gated run (the Approvals inbox, J7)
    paused = c.get("/v1/engine/team-runs", params={"state": "PAUSED"}).json()
    paused_ids = {r["id"] for r in paused["team_runs"]}
    assert paused_id in paused_ids and done_id not in paused_ids
    assert all(r["state"] == "PAUSED" for r in paused["team_runs"])

    # (4) pagination — a bounded page, with the FULL total for the table footer
    page = c.get("/v1/engine/team-runs", params={"limit": 1, "offset": 0}).json()
    assert len(page["team_runs"]) == 1 and page["total"] >= 2

    # (5) an unknown state is a 422 at the edge (never a silent empty list)
    assert c.get("/v1/engine/team-runs", params={"state": "BOGUS"}).status_code == 422


def test_the_list_is_org_isolated_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """User A's runs never appear in user B's list — RLS scopes the LIST to the request org."""
    a = gateway_client(register(f"listownera{uuid.uuid4().hex[:10]} user")["token"])
    paused_id, done_id = _seed_runs(a, tmp_path)

    b = gateway_client(register(f"listintruderb{uuid.uuid4().hex[:10]} user")["token"])
    b_body = b.get("/v1/engine/team-runs").json()
    b_ids = {r["id"] for r in b_body["team_runs"]}
    assert paused_id not in b_ids and done_id not in b_ids  # B cannot see A's runs
