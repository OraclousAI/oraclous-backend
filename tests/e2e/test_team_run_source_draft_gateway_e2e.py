"""#1163: a team run records the team draft + version it started from, END-TO-END through the API
GATEWAY on the DEPLOYED docker stack — NO fakes (FUCK_CLAUDE_FUCK_PAPERCLIP rule 5).

Keyless (rule 8: no fake LLM in a DoD proof, and no LLM at all is needed here): every team is a
gate-only team — its single member is a human gate (``{"role": "author-gate", "kind": "human",
"human_role": "author"}``) run with ``gate_decisions={"author-gate": "approve"}``. The orchestrator
settles an approved human gate as SUCCEEDED with no model call at all
(``packages/ohm/src/oraclous_ohm/orchestrate.py:576-582``), so this is a real, deployed-stack,
gateway-only proof that costs nothing and never touches OpenRouter.

Premise checked before writing this file: on a stack rebuilt from this worktree (services profile,
fresh images), a plain curl POST of exactly this manifest to ``/v1/engine/team-runs`` reaches
SUCCEEDED. RED reason confirmed on the same stack: a POST that also carries ``team_draft_id`` /
``team_draft_version`` is accepted (202) but the read has no ``team_draft_id`` key at all, and
``GET /v1/engine/team-drafts/{id}/succeeded-versions`` is a bare 404 (no such route yet).

Bring the stack up first: ``scripts/e2e.sh --up`` (auto-skips this suite when the gateway is down).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _team(org: str, members: list[dict]) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "source-draft-team",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


def _gate_team(org: str) -> dict:
    """The single, keyless member every test in this file runs: an approved human gate settles
    SUCCEEDED with no model call (rule 8)."""
    return _team(org, [{"role": "author-gate", "kind": "human", "human_role": "author"}])


def _poll(c: httpx.Client, run_id: str, tries: int = 160) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _create_draft(c: httpx.Client, org: str, name: str) -> dict:
    resp = c.post(
        "/v1/engine/team-drafts",
        json={"name": name, "manifest": _gate_team(org), "sub_harnesses": {}},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["draft"]


def test_a_team_run_records_its_team_and_the_team_lists_its_succeeded_version(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"srcdraft{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    org = user["org_id"]

    draft_a = _create_draft(c, org, "team A")
    draft_b = _create_draft(c, org, "team B (no runs)")
    assert draft_a["version"] == 1

    # a plain run with no draft fields — the wire pair is always present, and null
    plain = c.post(
        "/v1/engine/team-runs",
        json={"manifest": _gate_team(org), "sub_harnesses": {}, "gate_decisions": {}},
    )
    assert plain.status_code == 202, plain.text
    plain_read = c.get(f"/v1/engine/team-runs/{plain.json()['id']}").json()
    assert "team_draft_id" in plain_read, plain_read
    assert plain_read["team_draft_id"] is None
    assert "team_draft_version" in plain_read, plain_read
    assert plain_read["team_draft_version"] is None

    # a run against draft A, version 1 — the pair rides the run to SUCCEEDED
    run = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _gate_team(org),
            "sub_harnesses": {},
            "gate_decisions": {"author-gate": "approve"},
            "team_draft_id": draft_a["id"],
            "team_draft_version": 1,
        },
    )
    assert run.status_code == 202, run.text
    run_id = run.json()["id"]
    immediate = c.get(f"/v1/engine/team-runs/{run_id}").json()
    assert immediate["team_draft_id"] == draft_a["id"]
    assert immediate["team_draft_version"] == 1
    finished = _poll(c, run_id)
    assert finished["state"] == "SUCCEEDED", finished

    # the draft's own succeeded-versions read carries exactly that one version
    versions_a = c.get(f"/v1/engine/team-drafts/{draft_a['id']}/succeeded-versions")
    assert versions_a.status_code == 200, versions_a.text
    body_a = versions_a.json()
    assert body_a["total"] == 1, body_a
    assert body_a["versions"] == [
        {"version": 1, "team_run_id": run_id, "finished_at": body_a["versions"][0]["finished_at"]}
    ], body_a

    # a draft with no runs at all reads as an empty page, not a 404
    versions_b = c.get(f"/v1/engine/team-drafts/{draft_b['id']}/succeeded-versions")
    assert versions_b.status_code == 200, versions_b.text
    assert versions_b.json()["versions"] == []

    # the teams-with-a-success filter includes A and excludes B; unfiltered includes both
    filtered = c.get("/v1/engine/team-drafts", params={"has_succeeded_run": "true"}).json()
    filtered_ids = {row["id"] for row in filtered["team_drafts"]}
    assert draft_a["id"] in filtered_ids, filtered
    assert draft_b["id"] not in filtered_ids, filtered

    unfiltered = c.get("/v1/engine/team-drafts").json()
    unfiltered_ids = {row["id"] for row in unfiltered["team_drafts"]}
    assert {draft_a["id"], draft_b["id"]} <= unfiltered_ids, unfiltered


def test_a_stale_version_is_a_conflict_and_nothing_runs(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"staledraft{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    org = user["org_id"]

    draft = _create_draft(c, org, "stale-check team")
    bumped = c.put(
        f"/v1/engine/team-drafts/{draft['id']}",
        json={"name": "stale-check team v2", "manifest": _gate_team(org), "sub_harnesses": {}},
    )
    assert bumped.status_code == 200, bumped.text
    assert bumped.json()["draft"]["version"] == 2

    # a stale version (1) is refused before anything is dispatched
    stale = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _gate_team(org),
            "sub_harnesses": {},
            "gate_decisions": {"author-gate": "approve"},
            "team_draft_id": draft["id"],
            "team_draft_version": 1,
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "CONFLICT", stale.text
    assert c.get("/v1/engine/team-runs").json()["total"] == 0

    # the current version (2) runs
    current = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _gate_team(org),
            "sub_harnesses": {},
            "gate_decisions": {"author-gate": "approve"},
            "team_draft_id": draft["id"],
            "team_draft_version": 2,
        },
    )
    assert current.status_code == 202, current.text
    read = c.get(f"/v1/engine/team-runs/{current.json()['id']}").json()
    assert read["team_draft_version"] == 2

    # a partial pair (id without version) is refused with the named field
    incomplete = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _gate_team(org),
            "sub_harnesses": {},
            "gate_decisions": {"author-gate": "approve"},
            "team_draft_id": draft["id"],
        },
    )
    assert incomplete.status_code == 422, incomplete.text
    body = incomplete.json()
    assert body["error"]["code"] == "VALIDATION_FAILED", body
    assert {"field": "team_draft_version", "issue": "TEAM_DRAFT_REF_INCOMPLETE"} in body["error"][
        "details"
    ], body


def test_another_organisations_team_is_invisible(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    a = register(f"srcdrafta{uuid.uuid4().hex[:10]} u")
    b = register(f"srcdraftb{uuid.uuid4().hex[:10]} u")
    ca, cb = gateway_client(a["token"]), gateway_client(b["token"])

    draft = _create_draft(ca, a["org_id"], "a-only team")
    run = ca.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _gate_team(a["org_id"]),
            "sub_harnesses": {},
            "gate_decisions": {"author-gate": "approve"},
            "team_draft_id": draft["id"],
            "team_draft_version": 1,
        },
    )
    assert run.status_code == 202, run.text
    finished = _poll(ca, run.json()["id"])
    assert finished["state"] == "SUCCEEDED", finished

    # B posting a run against A's draft — invisible, not a real reference
    foreign = cb.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _gate_team(b["org_id"]),
            "sub_harnesses": {},
            "gate_decisions": {"author-gate": "approve"},
            "team_draft_id": draft["id"],
            "team_draft_version": 1,
        },
    )
    assert foreign.status_code == 422, foreign.text
    body = foreign.json()
    assert body["error"]["code"] == "VALIDATION_FAILED", body
    assert {"field": "team_draft_id", "issue": "INVALID_TEAM_DRAFT"} in body["error"]["details"], (
        body
    )

    # B reading A's draft's succeeded-versions — 404, same as any other foreign draft read
    assert cb.get(f"/v1/engine/team-drafts/{draft['id']}/succeeded-versions").status_code == 404

    # B's teams-with-a-success list never contains A's draft
    b_filtered = cb.get("/v1/engine/team-drafts", params={"has_succeeded_run": "true"}).json()
    assert draft["id"] not in {row["id"] for row in b_filtered["team_drafts"]}
