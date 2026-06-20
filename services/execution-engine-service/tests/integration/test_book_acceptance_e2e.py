"""E3 M1 book-shaped acceptance e2e (#440) on REAL Postgres + RLS — docker-required.

The whole chain a book GO exercises, end-to-end and automated: a book-shaped ``.claude/agents`` +
charter is **imported** (E2 ``import_setup``) into a runnable Team Harness + its sub-harnesses, the
team is **created + driven through the real worker path** (``TeamRunService.create``→``drive``) on
the org-bound ``oraclous_app`` engine, and the two bound acceptance items are demonstrated live:

- **Item 4b (blocking gate):** the run PAUSES at the human gate (the charter's hard gate → a human
  member) and only an ``advance`` crosses it — agents cannot.
- **Item 4 (capability-absence):** the imported generator has NO send/publish/spend tool, and a
  sub-harness that tries to smuggle one past its ``tools`` ceiling is rejected 422 (inline AND, via
  #436, the manifest_ref path is capped at the harness too).

This is the automated run-evidence; the §22 sign-off is the human pressing GO on the real book dir
(see ``scripts/book_acceptance_go.py``).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.team_run_service import TeamRunError, TeamRunService
from oraclous_governance import Principal, PrincipalType
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.organization_isolation]

ORG = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
USER = uuid.UUID("33333333-3333-3333-3333-333333333333")
_SEND = ("send", "publish", "upload", "spend")


def _principal() -> Principal:
    return Principal(principal_id=USER, principal_type=PrincipalType.USER, organisation_id=ORG)


class _FakeHarness:
    """Deterministic stand-in (the real loop is proven elsewhere): every member SUCCEEDS."""

    async def execute(
        self,
        *,
        input_text: str,
        manifest_inline: dict[str, Any] | None = None,
        manifest_ref: str | None = None,
        capability_ceiling: list[str] | None = None,
    ) -> dict[str, Any]:
        return {"status": "SUCCEEDED", "output": f"done: {input_text[:30]}"}


def _book_studio(root: Path) -> None:
    """A book-shaped setup: a researcher → a publisher generator (NO send tool) + a charter hard
    gate (the author approves) — the structure the real book/.claude/agents uses."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text(
        "---\nname: researcher\nmodel: opus\ntools: Read, WebSearch\n---\n"
        "Research the topic.\n## Handoff\n**Next agent**: publisher\n"
    )
    # the generator: Read + Write only — it literally cannot publish/upload/spend (the book rule)
    (agents / "publisher.md").write_text(
        "---\nname: publisher\nmodel: sonnet\ntools: Read, Write\n---\nDraft the chapter.\n"
    )
    team = root / "teams" / "1-book"
    team.mkdir(parents=True)
    (team / "charter.md").write_text(
        '# Team ① — Book ("studio")\n## Roster\n| Agent | Type | Model | Job |\n'
        "| --- | --- | --- | --- |\n| `researcher` | subagent | opus | research |\n"
        "| `publisher` | subagent | sonnet | draft |\n"
        "## Hard gates\n- **Gate A** — the author approves the outline.\n"
    )


async def _service(app_dsn: str) -> tuple[TeamRunService, TeamRunRepository]:
    repo = TeamRunRepository(app_dsn)
    return TeamRunService(team_runs=repo, harness=_FakeHarness()), repo


async def test_book_team_imports_runs_and_blocks_at_the_gate(engine_dsns, tmp_path: Path) -> None:  # noqa: ANN001
    """The book-shaped team imports to a runnable harness, drives through the worker on the real DB,
    PAUSES at the human gate (item 4b), and resumes to completion only on advance."""
    _owner, app_dsn = engine_dsns
    _book_studio(tmp_path)

    # E2: import the book-shaped dir -> a runnable Team Harness + its sub-harnesses
    imported = import_setup(tmp_path, owner_organization_id=ORG, name="book")
    assert imported.manifest is not None and imported.manifest.is_team()
    roles = {m.role: m for m in imported.manifest.members}
    assert any(m.kind == "human" for m in imported.manifest.members)  # the charter gate is a member
    assert set(imported.sub_harnesses) >= {"researcher", "publisher"}  # runnable bodies exposed

    svc, repo = await _service(app_dsn)
    try:
        # create + worker-drive on the real org-bound engine
        created = await svc.create(
            _principal(),
            manifest=imported.manifest.model_dump(mode="json"),
            sub_harnesses=imported.sub_harnesses,
            gate_decisions={},
        )
        assert created.state == "QUEUED"
        paused = await svc.drive(created.id, _principal())

        # Item 4b: the run is BLOCKED at the human gate — it did not run to completion
        assert paused.state == "PAUSED"
        gate_role = next(m.role for m in imported.manifest.members if m.kind == "human")
        assert paused.paused_at == [gate_role]

        # only an advance crosses the gate; then the run completes
        await svc.advance(paused.id, _principal(), {gate_role: "approve"})
        resumed = await svc.drive(paused.id, _principal())
        assert resumed.state == "SUCCEEDED"
    finally:
        await repo.close()

    # Item 4 (structural): the generator has NO send/publish/upload/spend capability
    pub = roles["publisher"]
    assert not (set(pub.tools) & set(_SEND))
    pub_sub = imported.sub_harnesses["publisher"]
    assert not ({c["binding"] for c in pub_sub["capabilities"]} & set(_SEND))


async def test_a_sub_harness_cannot_grant_the_generator_a_send_tool(
    engine_dsns,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    """Item 4 (enforced): a sub-harness that tries to smuggle a 'send' capability past the
    publisher's Read/Write ceiling is rejected 422 — no path grants an undeclared capability."""
    _owner, app_dsn = engine_dsns
    _book_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=ORG, name="book")
    assert imported.manifest is not None

    smuggled = dict(imported.sub_harnesses)
    smuggled["publisher"] = {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "publisher",
            "owner_organization_id": str(ORG),
        },
        "capabilities": [{"ref": "core/send@1", "binding": "send"}],  # outside [Read, Write]
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }

    svc, repo = await _service(app_dsn)
    try:
        with pytest.raises(TeamRunError) as exc:
            await svc.create(
                _principal(),
                manifest=imported.manifest.model_dump(mode="json"),
                sub_harnesses=smuggled,
                gate_decisions={},
            )
        assert exc.value.status_code == 422  # the ceiling rejected the send capability
    finally:
        await repo.close()
