"""Parse teams/<n>/charter.md into a structured CharterTeam (#406; ADR-034 §4).

Parse-only: roster rows, ## Hard gates (human blocking nodes), ## Handoff (edges), owns/writes.
Assembling these into a Team Harness (cross-referencing roster names to .claude/agents, building the
members[] DAG) is #408. Header-driven roster columns handle both the no-Verdict and Verdict shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_.charter import parse_charter, parse_charter_text

_TEAM3 = """# Team ③ — Writing & Editorial ("the craft")

## Purpose
Produce chapter drafts that advance the argument.

## Roster
| Agent | Type | Model | Job |
| --- | --- | --- | --- |
| `chapter-architect` | subagent | opus | One TOC chapter to a beat outline. |
| `narrative-drafter` | subagent | sonnet | Prose from the outline. |

## Owns / writes
- `outline/chapters/` — chapter-architect (beat outlines).
- `drafts/` — narrative-drafter (prose).

## Handoff & gates
- After the memo → **Gate B** (author: revise / re-outline).
- `drafts/CH-XX.md` → Team ④ for fact-check.
"""

_TEAM5 = """# Team ⑤ — Production ("the ship")

## Purpose
Ship the manuscript.

## Roster
| Agent | Type | Model | Verdict | Job |
| --- | --- | --- | --- | --- |
| `book-formatter` | skill | sonnet | AI | Format to epub/pdf. |
| `cover-designer` | subagent | haiku | human-or-outsource | Design the cover. |

## Owns / writes
Writes `production/` — `manuscript/`, `formats/{epub,print-pdf,kpf}`.

## Hard gates
- **Gate E** — the author uploads the final files.
"""


def test_parses_header() -> None:
    ct = parse_charter_text(_TEAM3)
    assert ct.team_num == 3
    assert ct.team_name == "Writing & Editorial"
    assert ct.subtitle == "the craft"


def test_parses_purpose() -> None:
    assert "Produce chapter drafts" in parse_charter_text(_TEAM3).purpose


def test_roster_no_verdict_column() -> None:
    ct = parse_charter_text(_TEAM3)
    assert [r.agent_name for r in ct.roster] == ["chapter-architect", "narrative-drafter"]
    assert ct.roster[0].type == "subagent"
    assert ct.roster[0].model == "opus"
    assert all(r.verdict is None for r in ct.roster)  # no Verdict column present
    assert "beat outline" in ct.roster[0].job


def test_roster_with_verdict_column() -> None:
    ct = parse_charter_text(_TEAM5)
    assert {r.agent_name: r.verdict for r in ct.roster} == {
        "book-formatter": "AI",
        "cover-designer": "human-or-outsource",
    }
    assert ct.roster[0].type == "skill"


def test_owns_writes_bullets() -> None:
    paths = [w.path for w in parse_charter_text(_TEAM3).owns_writes]
    assert "outline/chapters/" in paths
    assert "drafts/" in paths


def test_owns_writes_prose_tokenized() -> None:
    paths = [w.path for w in parse_charter_text(_TEAM5).owns_writes]
    assert "production/" in paths
    assert any("formats/{epub" in p for p in paths)  # brace-set path preserved verbatim


def test_hard_gates() -> None:
    ct = parse_charter_text(_TEAM5)
    assert [g.gate_id for g in ct.hard_gates] == ["E"]
    assert "author uploads" in ct.hard_gates[0].description


def test_handoff_edges() -> None:
    ct = parse_charter_text(_TEAM3)
    assert "B" in {h.gate_ref for h in ct.handoffs}  # internal gate edge
    assert 4 in {h.to_team for h in ct.handoffs}  # cross-team edge to Team ④


def test_no_heading_fails_closed() -> None:
    with pytest.raises(OHMImportError):
        parse_charter_text("just text, no markdown heading at all")


def test_header_but_no_roster_flags_blocking() -> None:
    ct = parse_charter_text('# Team ① — Insight ("x")\n\n## Purpose\nthink.\n')
    assert ct.roster == []
    assert {f.code: f.severity for f in ct.flags}.get("F-CHARTER-NOROSTER") == "blocking"


def test_parse_charter_file(tmp_path: Path) -> None:
    p = tmp_path / "charter.md"
    p.write_text(_TEAM3)
    assert parse_charter(p).team_num == 3
