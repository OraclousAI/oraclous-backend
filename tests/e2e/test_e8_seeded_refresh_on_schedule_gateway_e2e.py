"""#544 (E8) — seeded-refresh ON A SCHEDULE: a standing team's fire N+1 SEEDS from fire N, LIVE.

ADR-048 O7 (the EURail seeded-refresh-on-a-cron form): a standing ``target_kind="team"`` schedule
auto-seeds each recurring fire from its LAST SUCCEEDED fire — so run N+1 carries forward run N's
records (the #602 5-way delta) instead of a cold rebuild every tick. This is the auto-wire joining
#601 (standing-team fire) + #602 (seeded-refresh) on the schedule path; the github-sink's clean
re-delivery is already recurrence-safe (#515), so it is not re-proven here.

No fakes, through the gateway ``:8006`` with real OpenRouter BYOM: register a standing reporter team
(emits a fixed JSON ledger) bound to a graph → fire window N (COLD: ``seed_from_run_id`` null,
``refresh_delta`` null) → wait it settle SUCCEEDED (→ stamped as the schedule's seed) → cross the
cron minute boundary → fire window N+1 → assert a later run SEEDED from a prior SUCCEEDED fire
(``seed_from_run_id`` set) and settled with a first-class ``refresh_delta`` (the recurring what-
changed contract). Beat-tolerant: Beat's own ``* * * * *`` fires also seed, only strengthening it.
Auto-skips without the BYOM key / a reachable gateway.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_TERMINAL = {"SUCCEEDED", "FAILED", "PARTIAL", "REJECTED", "COST_BUDGET"}
_LEDGER = '[{"id":"a","fact":"alpha"},{"id":"b","fact":"bravo"},{"id":"c","fact":"charlie"}]'


def _byom_model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _cred(c: httpx.Client, user: dict) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "byom",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bare_graph(c: httpx.Client) -> str:
    # a team schedule requires a graph binding; the reporter never reads it, so no ingest is needed.
    g = c.post("/api/v1/graphs", json={"name": "refresh-sched-kb", "description": "#544 e2e"})
    assert g.status_code == 201, g.text
    return g.json()["id"]


def _reporter_studio(root: Path, ledger: str) -> None:
    """A one-member standing 'reporter' told to emit an EXACT JSON ledger (so the delta is real)."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "You are a data reporter. Output the following JSON array of records EXACTLY as given, "
        f"verbatim, with no changes: {ledger}\n\nReply with ONLY that JSON array — no prose, no "
        "fences, no explanation."
    )
    (agents / "reporter.md").write_text(f"---\nname: reporter\nmodel: sonnet\n---\n{body}\n")
    (root / "teams" / "1-report").mkdir(parents=True)
    (root / "teams" / "1-report" / "charter.md").write_text(
        "# Team I — Report\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `reporter` | subagent | sonnet | report |\n"
    )


def _runs(c: httpx.Client, sched_id: str) -> list[dict]:
    r = c.get(f"/v1/engine/schedules/{sched_id}/team-runs")
    assert r.status_code == 200, r.text
    return r.json()["runs"]


def _detail(c: httpx.Client, run_id: str) -> dict:
    return c.get(f"/v1/engine/team-runs/{run_id}").json()


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = _detail(c, run_id)
        if row["state"] in _TERMINAL:
            return row
        time.sleep(2)
    raise AssertionError(f"team-run {run_id} never settled (last: {row.get('state')})")


@requires_byom
def test_standing_team_recurring_fire_seeds_from_the_prior_fire(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"refsched{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])
    graph_id = _bare_graph(c)

    _reporter_studio(tmp_path, _LEDGER)
    imported = import_setup(tmp_path, owner_organization_id=org, name="refsched")
    assert imported.manifest is not None
    manifest = imported.manifest.model_dump(mode="json")
    manifest["models"] = [_byom_model(cred)]
    sub_harnesses = {
        role: {**dict(sub), "models": [_byom_model(cred)]}
        for role, sub in imported.sub_harnesses.items()
    }
    reg = c.post(
        "/v1/engine/schedules",
        json={
            "type": "cron",
            "cron": "* * * * *",
            "target_kind": "team",
            "manifest": manifest,
            "input": "standing refresh",
            "input_data": {"sub_harnesses": sub_harnesses, "gate_decisions": {}},
            "graph_id": graph_id,
        },
    )
    assert reg.status_code == 201, reg.text
    sched_id = reg.json()["id"]

    # ── FIRE WINDOW N (own a fresh minute so it is one clean window) — a COLD build ───────────────
    time.sleep(60 - datetime.now(UTC).second + 1)  # just past a boundary → a fresh window
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()
    time.sleep(3)
    first = _runs(c, sched_id)
    assert first, "fire N created a run"
    n_id = first[0]["id"]
    n_row = _poll(c, n_id)  # settle N SUCCEEDED → it is stamped as the schedule's seed
    assert n_row["state"] == "SUCCEEDED", f"fire N must settle SUCCEEDED — {n_row}"
    # the FIRST fire is COLD — no prior to seed from, so the engine emits no refresh_delta (a seeded
    # fire's delta IS its seed proof; refresh_delta is only computed when seed_from_run_id is set).
    assert n_row.get("refresh_delta") is None, f"a cold fire emits no delta — {n_row}"

    # ── FIRE WINDOW N+1 (cross the cron boundary → a NEW window) — SEEDED from N ──────────────────
    time.sleep(60 - datetime.now(UTC).second + 2)  # into the next minute
    c.post(f"/v1/engine/schedules/{sched_id}/fire-now").raise_for_status()

    # a LATER fire SEEDED from a prior SUCCEEDED run + settled with a first-class refresh_delta —
    # poll the run set until it appears + settles (Beat's autonomous fires also seed — superset).
    # the refresh_delta on the run DETAIL IS the seed proof — only a SEEDED fire emits it (it is
    # gated on seed_from_run_id at settle), and the delta carries its own ``seed_from_run_id``. The
    # LIST DTO does not surface seed_from_run_id, so we key off the delta.
    seeded: dict | None = None
    for _ in range(90):
        for run in _runs(c, sched_id):
            if run["id"] == n_id:
                continue
            row = _detail(c, run["id"])
            if row["state"] == "SUCCEEDED" and row.get("refresh_delta") is not None:
                seeded = row
                break
        if seeded is not None:
            break
        time.sleep(2)
    assert seeded is not None, (
        "a recurring fire must SEED from the prior fire + emit a refresh_delta"
    )

    # THE #544 CONTRACT: the recurring fire seeded from a real prior SUCCEEDED fire of the schedule,
    # and the engine emitted the 5-way what-changed delta keyed to it (not a cold rebuild).
    delta = seeded["refresh_delta"]
    seed_id = delta.get("seed_from_run_id")
    assert seed_id, f"the delta names its seed run — {delta}"
    assert str(seed_id) in {r["id"] for r in _runs(c, sched_id)}, (
        "seeded from a run OF THIS SCHEDULE"
    )
    assert delta.get("records_parsed", True), (
        f"the deliverable is a record-set → a real delta — {delta}"
    )
    # same fixed ledger each fire → every record carries (re_confirmed/unchanged), NONE spuriously
    # added/removed — the cheap-refresh signal the recurring form exists for.
    assert delta.get("counts", {}).get("added", 0) == 0, (
        f"no spurious adds on a stable ledger — {delta}"
    )
    assert not delta.get("removed"), f"no spurious removals on a stable ledger — {delta}"
