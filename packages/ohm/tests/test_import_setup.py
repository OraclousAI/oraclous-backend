"""import_setup + O8 dry-run report (#409; ADR-034 §1/§7).

The single front door: discover a setup directory, run the whole importer, and emit a read-only
dry-run report (parsed team + DAG + skills resolved/failed + flags by severity) before any live run.
Validated
across both setup shapes (an agent-team with charters + cron, and a single orchestrator skill).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from oraclous_ohm.import_.setup import import_setup, render_report
from oraclous_ohm.parse import load_ohm

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _agent_team(root: Path) -> None:
    ag = root / ".claude" / "agents"
    ag.mkdir(parents=True)
    (ag / "a.md").write_text(
        "---\nname: a\nmodel: opus\ntools: Read, Write\nskills:\n  - helper\n---\n"
        "You are a.\n## Handoff\n**Next agent**: b\n"
    )
    (ag / "b.md").write_text("---\nname: b\nmodel: sonnet\ntools: Read\n---\nYou are b.\n")
    sk = root / ".claude" / "skills" / "helper"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: helper\ndescription: A helper.\n---\nDo helpful things."
    )
    tm = root / "teams" / "1-x"
    tm.mkdir(parents=True)
    (tm / "charter.md").write_text(
        '# Team ① — X ("y")\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n'
        "| `a` | subagent | opus | do a |\n## Hard gates\n- **Gate D** — the author approves.\n"
    )
    (root / "harness").mkdir(parents=True)
    (root / "harness" / "cron.yaml").write_text(
        'jobs:\n  - id: daily\n    cron: "0 9 * * *"\n    agent: a\n'
    )


def _orchestrator(parent: Path) -> Path:
    sk = parent / "myorch"
    (sk / "modules" / "research").mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: myorch\ndescription: Fans out research subagents.\n---\n## Phases\nwaves.\n"
    )
    (sk / "modules" / "research" / "01-x.md").write_text("# x brief\n")
    return sk


def test_agent_team_imports_and_loads(tmp_path: Path) -> None:
    _agent_team(tmp_path)
    result = import_setup(tmp_path, owner_organization_id=_ORG, name="demo")
    assert result.report.shape == "agent-team"
    assert result.report.member_count == 3  # a, b, gate-d
    assert result.report.human_gate_count == 1  # the Gate D human member
    assert result.manifest is not None
    loaded = load_ohm(result.manifest.model_dump(mode="json"))  # the assembled team loads
    assert loaded.is_team()


def test_skill_inlined_and_schedule_attached(tmp_path: Path) -> None:
    _agent_team(tmp_path)
    result = import_setup(tmp_path, owner_organization_id=_ORG)
    assert result.report.resolved_skills >= 1  # helper inlined into a
    assert result.report.schedules == {"a": "0 9 * * *"}  # cron discovered + attached


def test_handoff_becomes_pipeline_dag(tmp_path: Path) -> None:
    _agent_team(tmp_path)
    result = import_setup(tmp_path, owner_organization_id=_ORG)
    by = {m.role: m for m in result.manifest.members}  # type: ignore[union-attr]
    assert by["b"].depends_on == ["a"]  # a hands to b


def test_orchestrator_shape(tmp_path: Path) -> None:
    sk = _orchestrator(tmp_path)
    result = import_setup(sk, owner_organization_id=_ORG)
    assert result.report.shape == "orchestrator"
    assert {m.role for m in result.manifest.members} == {"x"}  # type: ignore[union-attr]


def test_empty_dir_blocks(tmp_path: Path) -> None:
    result = import_setup(tmp_path, owner_organization_id=_ORG)
    assert result.manifest is None
    assert result.report.shape == "none"
    assert result.report.would_block is True
    assert any("F-NO-SETUP" in b for b in result.report.blocking)


def test_render_report_is_human_readable(tmp_path: Path) -> None:
    _agent_team(tmp_path)
    text = render_report(import_setup(tmp_path, owner_organization_id=_ORG).report)
    assert "Import dry-run" in text
    assert "GO:" in text
    assert "members:" in text


def _two_team(root: Path) -> None:
    ag = root / ".claude" / "agents"
    ag.mkdir(parents=True)
    (ag / "scout.md").write_text("---\nname: scout\ntools: Read\n---\nresearch.")
    (ag / "writer.md").write_text("---\nname: writer\ntools: Write\n---\nwrite.")
    for num, team, agent in [("1", "research", "scout"), ("2", "writing", "writer")]:
        t = root / "teams" / f"{num}-{team}"
        t.mkdir(parents=True)
        (t / "charter.md").write_text(
            f'# Team {num} — {team} ("x")\n## Roster\n| Agent | Type | Model | Job |\n'
            f"| --- | --- | --- | --- |\n| `{agent}` | subagent | opus | do |\n"
        )


def test_charter_team_pipeline_no_handoffs(tmp_path: Path) -> None:
    # book-shaped: no ## Handoff edges; structure comes from the numbered charters
    _two_team(tmp_path)
    result = import_setup(tmp_path, owner_organization_id=_ORG)
    by = {m.role: m for m in result.manifest.members}  # type: ignore[union-attr]
    assert by["writer"].depends_on == ["scout"]  # team 2 depends_on team 1
    assert result.manifest.execution_stages() == [["scout"], ["writer"]]  # type: ignore[union-attr]
    assert any(f.code == "F-CHARTER-PIPELINE" for f in result.flags)
