"""#577 slice-3 — the reader-panel uv-CLI skill is STAGED as an OHMRuntime.driver, not inlined.

reader-panel is a real uv package (a ``pyproject.toml`` ``[project.scripts]`` + a ``__main__.py``).
The importer drops its cwd/venv/env/entry today (a leaf skill is inlined as prose, which a CLI is
not).
slice-3 detects it as a ``driver`` skill and records the staging contract on the sub-harness's
``runtime.driver`` — RECORDED, never executed (the venv creation + dispatch is the harness-runtime's
job, out of this importer slice).

Built against the REAL fixture. RED until the [impl] adds ``SkillKind`` ``"driver"`` +
``OHMSkillDriver`` + ``OHMRuntime.driver`` + the pyproject detection/staging.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from oraclous_ohm.import_.mapping import map_agent_to_member
from oraclous_ohm.import_.parse import AgentDefinition
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_SKILLS = Path(__file__).parent / "fixtures" / "book-team" / ".claude" / "skills"


def _agent(skills: list[str]) -> AgentDefinition:
    return AgentDefinition(
        name="panel-runner",
        description="Runs the synthetic reader panel.",
        model="opus",
        tools=["Read"],
        skills=skills,
        body="You run the synthetic reader panel for a chapter.",
        source="panel-runner.md",
    )


def test_reader_panel_resolves_as_a_driver() -> None:
    from oraclous_ohm.import_.skills import resolve_skill

    resolved = resolve_skill("reader-panel", _SKILLS)
    # a uv package with [project.scripts] is a DRIVER (precedence: driver > orchestrator > leaf)
    assert resolved.kind == "driver"


def test_driver_is_staged_on_the_sub_harness_runtime_with_the_real_contract() -> None:
    m = map_agent_to_member(
        _agent(["reader-panel"]), owner_organization_id=_ORG, skills_root=_SKILLS
    )
    assert "F-SKILL-DRIVER" in {f.code for f in m.flags}
    assert m.sub_harness is not None
    driver = m.sub_harness.runtime.driver
    assert driver is not None
    assert driver.command_name == "reader-panel"  # [project.scripts] LHS
    assert driver.entry_point == "reader_panel.cli:main"  # [project.scripts] RHS
    assert driver.package_path == "reader-panel"  # the team-root sibling package
    assert "ANTHROPIC_API_KEY" in driver.env  # the SKILL.md Setup env (runtime-injected)


def test_driver_skill_is_not_inlined_into_the_prompt() -> None:
    # a driver is staged, NOT flattened into the prompt like a leaf skill (a CLI is not prose).
    m = map_agent_to_member(
        _agent(["reader-panel"]), owner_organization_id=_ORG, skills_root=_SKILLS
    )
    loaded = load_ohm(m.sub_harness.model_dump(mode="json"))
    assert "## Available Skills" not in loaded.primary_prompt().body  # type: ignore[union-attr]
