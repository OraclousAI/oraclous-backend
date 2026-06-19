"""Parse a .claude/agents/*.md into an AgentDefinition (issue #405; ADR-034 §1, the import front door).

The parser is tolerant of the real, loosely-specified .claude/agents format: ``tools`` may be a YAML
list OR a comma string; ``skills`` is optional; the markdown body (the agent's system prompt) is the
rest after the frontmatter. It fails closed on a missing ``name`` or absent frontmatter.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_.parse import AgentDefinition, discover_agents, parse_agent_text

_AGENT = textwrap.dedent("""\
    ---
    name: diagram-generator
    description: Team 5 visual-aid drafter.
    tools: Read, Grep, Glob, Write
    model: sonnet
    ---

    You are diagram-generator. Read AGENTS.md first.

    ## Write scope
    - production/diagrams/ only.
""")


def test_parses_frontmatter_and_body() -> None:
    a = parse_agent_text(_AGENT, source="diagram-generator.md")
    assert isinstance(a, AgentDefinition)
    assert a.name == "diagram-generator"
    assert a.model == "sonnet"
    assert a.tools == ["Read", "Grep", "Glob", "Write"]  # comma string -> normalized list
    assert a.skills == []  # optional
    assert a.description.startswith("Team 5")
    assert "## Write scope" in a.body  # body retained
    assert not a.body.startswith("---")  # frontmatter stripped
    assert a.source == "diagram-generator.md"


def test_tools_as_yaml_list() -> None:
    text = "---\nname: x\ntools:\n  - Read\n  - Write\n---\nbody"
    assert parse_agent_text(text).tools == ["Read", "Write"]


def test_tools_absent_is_empty_ceiling() -> None:
    text = "---\nname: x\n---\nbody"
    assert parse_agent_text(text).tools == []


def test_skills_captured() -> None:
    text = "---\nname: x\nskills:\n  - graphify-aware\n  - evidence-ledger\n---\nbody"
    assert parse_agent_text(text).skills == ["graphify-aware", "evidence-ledger"]


def test_missing_name_fails_closed() -> None:
    with pytest.raises(OHMImportError):
        parse_agent_text("---\ndescription: no name here\n---\nbody")


def test_no_frontmatter_fails_closed() -> None:
    with pytest.raises(OHMImportError):
        parse_agent_text("just a body, no frontmatter at all")


def test_discover_agents_finds_md_sorted(tmp_path: Path) -> None:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "b.md").write_text("---\nname: b\n---\nbody")
    (agents / "a.md").write_text(_AGENT)
    (agents / "README.txt").write_text("not an agent")
    found = discover_agents(agents)
    assert [p.name for p in found] == ["a.md", "b.md"]  # sorted, *.md only
