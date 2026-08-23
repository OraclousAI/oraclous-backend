"""#828 criterion 5 — the status response CHANGES between start and settle, on the deployed stack.

Every other ``/status`` assertion in this repo is taken after the run has settled, so none of them
can tell a run view from a stopwatch. This one polls a live multi-stage run through the
application-gateway and asserts the response moved while the run was still driving. That is the
whole issue in one test: today a client can say "started" and "finished" and nothing in between.

Per the DEPLOYED-STACK VERIFICATION LAW (CLAUDE.md §9) everything here goes through the gateway on
:8006 with a real JWT from a real registration. Real engine, real Celery worker, real harness. No
fakes, no internal calls, no DB-direct assertions.

Bring the stack up first:
    docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev-ports.yml up -d
Then: uv run pytest tests/e2e -m e2e   (auto-skips when the gateway is unreachable)

RED until the dispatch-time write, the timing columns and the role map land.
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

_TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "COST_BUDGET"}


def _three_stage_studio(root: Path) -> None:
    """Three members in a strict chain, reasoning-only so it runs on the deployed harness without
    tool resolution. A chain rather than a fan-out on purpose: with each member gated on the last,
    there is always exactly one in flight, so 'which member is running' has a single right answer
    the assertions below can check against the roster."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    for name, job in (
        ("researcher", "Research the topic and propose an outline."),
        ("writer", "Draft the chapter from the outline."),
        ("editor", "Tighten the draft and return the final text."),
    ):
        (agents / f"{name}.md").write_text(f"---\nname: {name}\nmodel: sonnet\n---\n{job}\n")
    (root / "teams" / "1-research").mkdir(parents=True)
    (root / "teams" / "1-research" / "charter.md").write_text(
        "# Team I — Research\n## Roster\n"
        "| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `researcher` | subagent | sonnet | research |\n"
    )
    (root / "teams" / "2-write").mkdir(parents=True)
    (root / "teams" / "2-write" / "charter.md").write_text(
        "# Team II — Write\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `writer` | subagent | sonnet | draft |\n"
        "| `editor` | subagent | sonnet | edit |\n"
    )


def _sample_status(client: httpx.Client, run_id: str, *, tries: int = 300) -> list[dict]:
    """Poll /status until the run settles, keeping every response that differs from the last.

    Measured on the deployed stack: three reasoning-only sonnet members in a strict chain can
    settle the WHOLE run in well under a second (one observed run: 0.66s total). A poll cadence
    built for "well inside the 10-15s a real client would use" misses that window almost every
    time — it is not a slow-client margin, it is a coin flip against the run's actual lifetime.
    0.25s keeps the same ~75s ceiling (300 tries) while giving several samples inside even a
    sub-second run, so criterion 5 is asserting on the signal rather than on scheduler luck.
    """
    samples: list[dict] = []
    for _ in range(tries):
        body = client.get(f"/v1/engine/team-runs/{run_id}/status").json()
        if not samples or body != samples[-1]:
            samples.append(body)
        if body["state"] in _TERMINAL:
            return samples
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} never settled (last: {samples[-1] if samples else None})")


def _start_run(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> tuple[httpx.Client, str, list[str]]:
    _three_stage_studio(tmp_path)
    imported = import_setup(
        tmp_path, owner_organization_id=uuid.uuid4(), name="studio", substrate="file"
    )
    assert imported.manifest is not None
    roster = [m.role for m in imported.manifest.members]

    c = gateway_client(register(f"midrun{uuid.uuid4().hex[:10]} user")["token"])
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": imported.sub_harnesses,
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text
    return c, created.json()["id"], roster


def test_the_status_response_changes_while_the_run_is_still_driving(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Criterion 5, and criterion 1 alongside it. A member must read ``running`` at least once
    while the run is live, and at least one sample must land before the terminal one."""
    c, run_id, roster = _start_run(tmp_path, register, gateway_client)

    samples = _sample_status(c, run_id)

    mid_run = [s for s in samples if s["state"] not in _TERMINAL]
    assert mid_run, "every sample was terminal: the run emitted nothing between start and settle"

    running_roles = {
        role
        for sample in mid_run
        for role, status in (sample.get("member_status") or {}).items()
        if status == "running"
    }
    assert running_roles, "no member ever reported running"
    assert running_roles <= set(roster), (
        f"a non-member reported running: {running_roles - set(roster)}"
    )


def test_a_running_member_never_inflates_progress(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """The honesty half of the issue. ``progress`` must never run ahead of the members that have
    actually delivered — a dispatched member is work started, not work done."""
    c, run_id, roster = _start_run(tmp_path, register, gateway_client)

    for sample in _sample_status(c, run_id):
        statuses = sample.get("member_status") or {}
        delivered = sum(1 for s in statuses.values() if s in ("succeeded", "skipped", "partial"))
        ceiling = round(100 * delivered / len(roster))
        assert sample["progress"] <= ceiling, (
            f"progress {sample['progress']} runs ahead of {delivered}/{len(roster)} delivered"
        )


def test_a_member_reports_how_long_it_has_been_running(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Criterion 2. An in-flight member carries a start and no end; a settled one carries both."""
    import datetime as _dt

    c, run_id, _roster = _start_run(tmp_path, register, gateway_client)
    samples = _sample_status(c, run_id)

    in_flight = [
        (role, window)
        for sample in samples
        if sample["state"] not in _TERMINAL
        for role, window in (sample.get("member_timings") or {}).items()
        if (sample.get("member_status") or {}).get(role) == "running"
    ]
    assert in_flight, "no member carried a timing window while it was running"
    for role, window in in_flight:
        assert window["started_at"] is not None, f"{role} was running with no start time"
        assert window["ended_at"] is None, f"{role} was running with an end time already set"

    final = samples[-1]
    assert final["state"] in _TERMINAL
    for role, window in (final.get("member_timings") or {}).items():
        if (final["member_status"] or {}).get(role) in ("succeeded", "partial", "failed"):
            started = _dt.datetime.fromisoformat(window["started_at"])
            ended = _dt.datetime.fromisoformat(window["ended_at"])
            assert ended >= started, f"{role} ended before it started"


def test_the_tree_maps_each_child_execution_to_its_member(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Criterion 4, through the gateway on a real run: every dispatched execution is attributable
    to the member that produced it, which is what per-member drill-down needs."""
    c, run_id, roster = _start_run(tmp_path, register, gateway_client)
    _sample_status(c, run_id)

    tree = c.get(f"/v1/engine/team-runs/{run_id}/tree").json()
    children = tree["children"]
    assert children, "the run dispatched no child executions"
    assert {child["execution_id"] for child in children} == set(tree["child_execution_ids"])
    labelled = {child["role"] for child in children if child["role"] is not None}
    assert labelled, "no child execution carried a role"
    assert labelled <= set(roster), f"a child claimed a non-member role: {labelled - set(roster)}"
