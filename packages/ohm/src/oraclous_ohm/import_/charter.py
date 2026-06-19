"""Parse a ``teams/<n>/charter.md`` into a structured ``CharterTeam`` (#406; ADR-034 §4).

PARSE-ONLY. This extracts the roster, the human ``## Hard gates``, the ``## Handoff`` edges, and the
owns/writes scope into a typed object. Assembling them into a Team Harness — cross-referencing the
roster to ``.claude/agents/*.md`` and building the ``members[]`` DAG with ``kind: human`` gate nodes
— is #408, which consumes this. So this module never imports the agent mapper or skill resolver; the
join lives in #408. Pure; fail-closed; flag-not-guess (reuses the shared ``ImportFlag``).
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_._flags import ImportFlag

_CIRCLED = {c: i + 1 for i, c in enumerate("①②③④⑤⑥⑦⑧⑨⑩")}
_DASH = "—"  # the em-dash the charters use to separate a path/gate from its note
_GATE_RE = re.compile(r"\*\*Gate\s+([A-Z])\b[^*]*\*\*")
_SEP_RE = re.compile(r"^:?-+:?$")


class RosterEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_name: str  # backticks stripped
    type: str = ""  # free-text ("subagent" | "skill" | ...); not enumerated (real data varies)
    model: str = ""  # verbatim, may read e.g. "sonnet (opus for marquee chapters)"
    verdict: str | None = None  # None when the Verdict column is absent; else the verbatim cell
    job: str = ""


class WritesScope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str  # backticks stripped; brace-sets like formats/{epub,print-pdf} kept verbatim
    note: str = ""


class HardGate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gate_id: str  # the letter from **Gate X** — a human-blocking-node descriptor for #408
    description: str = ""


class Handoff(BaseModel):
    model_config = ConfigDict(extra="ignore")

    to_team: int | None = None  # destination team number, None if the line names none
    raw: str = ""
    gate_ref: str | None = None  # gate letter if the line names a **Gate X**


class CharterTeam(BaseModel):
    model_config = ConfigDict(extra="ignore")

    team_num: int | None = None
    team_name: str = ""
    subtitle: str = ""
    purpose: str = ""
    roster: list[RosterEntry] = Field(default_factory=list)
    owns_writes: list[WritesScope] = Field(default_factory=list)
    hard_gates: list[HardGate] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    flags: list[ImportFlag] = Field(default_factory=list)
    source: str = "<unknown>"


def _team_num(token: str) -> int | None:
    token = token.strip()
    if token in _CIRCLED:
        return _CIRCLED[token]
    return int(token) if token.isdigit() else None


def _sections(text: str) -> dict[str, list[str]]:
    """Map each ``## `` heading (lowercased) to its body lines, up to the next heading."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = buf
            current, buf = line[3:].strip().lower(), []
        elif line.startswith("# "):
            if current is not None:
                sections[current] = buf
            current, buf = None, []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = buf
    return sections


def _parse_header(h1: str, flags: list[ImportFlag]) -> tuple[int | None, str, str]:
    content = h1[2:].strip()  # drop "# "
    team_num: int | None = None
    tm = re.match(r"Team\s+(\S+)\s*[—-]\s*(.*)$", content)
    rest = tm.group(2) if tm else content
    if tm:
        team_num = _team_num(tm.group(1))
    subtitle = ""
    sm = re.search(r'\(["“]([^"”]+)["”]\)', rest)
    if sm:
        subtitle = sm.group(1)
        team_name = rest[: sm.start()].strip()
    else:
        team_name = rest.strip()
    if team_num is None:
        flags.append(_flag("F-CHARTER-NOTEAMNUM", "confirm", "no team number parsed from the H1"))
    return team_num, team_name, subtitle


def _flag(code: str, severity: str, message: str, member_role: str = "") -> ImportFlag:
    return ImportFlag(code=code, severity=severity, member_role=member_role, message=message)  # type: ignore[arg-type]


def _cell(cells: list[str], cols: dict[str, int], name: str) -> str:
    """The named column's cell for a roster row (header-driven; '' if absent)."""
    i = cols.get(name)
    return cells[i].strip() if i is not None and i < len(cells) else ""


def _parse_roster(lines: list[str], flags: list[ImportFlag]) -> list[RosterEntry]:
    rows = [ln for ln in lines if ln.strip().startswith("|")]
    if len(rows) < 2:
        return []
    header = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    cols = {h.lower(): i for i, h in enumerate(header)}
    job_idx = cols.get("job", len(header) - 1)
    entries: list[RosterEntry] = []
    for row in rows[1:]:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if cells and all(_SEP_RE.match(c) for c in cells if c):
            continue  # the |---| separator row
        agent_name = cells[cols["agent"]] if "agent" in cols and cols["agent"] < len(cells) else ""
        agent_name = agent_name.strip("`").strip()
        if not agent_name:
            continue
        entries.append(
            RosterEntry(
                agent_name=agent_name,
                type=_cell(cells, cols, "type"),
                model=_cell(cells, cols, "model"),
                verdict=(_cell(cells, cols, "verdict") or None) if "verdict" in cols else None,
                job=cells[job_idx].strip() if job_idx < len(cells) else "",
            )
        )
        if len(cells) != len(header):
            flags.append(
                _flag("F-CHARTER-ROSTERROW", "confirm", "row column-count mismatch", agent_name)
            )
    return entries


def _parse_writes(lines: list[str], flags: list[ImportFlag]) -> list[WritesScope]:
    scopes: list[WritesScope] = []
    bullets = [ln for ln in lines if ln.strip().startswith("- ")]
    if bullets:
        for b in bullets:
            note = b.split(_DASH, 1)[1].strip() if _DASH in b else ""
            for p in re.findall(r"`([^`]+)`", b):
                if "/" in p:
                    scopes.append(WritesScope(path=p, note=note))
    else:
        for p in re.findall(r"`([^`]+)`", "\n".join(lines)):
            if "/" in p:
                scopes.append(WritesScope(path=p, note=""))
        if scopes:
            flags.append(
                _flag("F-CHARTER-WRITESPROSE", "info", "writes scope tokenized from prose")
            )
    return scopes


def _parse_hard_gates(text: str, flags: list[ImportFlag]) -> list[HardGate]:
    gates: list[HardGate] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = _GATE_RE.search(line)
        if not m:
            continue
        gid = m.group(1)
        if gid in seen:
            flags.append(_flag("F-CHARTER-DUPGATE", "info", f"Gate {gid} referenced again"))
            continue
        seen.add(gid)
        desc = line.split(_DASH, 1)[1].strip() if _DASH in line else line[m.end() :].strip()
        gates.append(HardGate(gate_id=gid, description=desc))
    return gates


def _parse_handoffs(lines: list[str], flags: list[ImportFlag]) -> list[Handoff]:
    handoffs: list[Handoff] = []
    for line in lines:
        if "→" not in line:
            continue
        after = line.rsplit("→", 1)[1]
        tm = re.search(r"Team\s+(\S+)", after)
        to_team = _team_num(tm.group(1)) if tm else None
        gate = _GATE_RE.search(line)
        if to_team is None and gate is None:
            flags.append(
                _flag("F-CHARTER-HANDOFFNOTEAM", "confirm", "handoff names no team or gate")
            )
        handoffs.append(
            Handoff(
                to_team=to_team,
                raw=line.strip().lstrip("- ").strip(),
                gate_ref=(gate.group(1) if gate else None),
            )
        )
    return handoffs


def parse_charter_text(text: str, source: str = "<unknown>") -> CharterTeam:
    """Parse a charter into a ``CharterTeam`` (structured parse only — assembly is #408)."""
    h1 = next((ln for ln in text.splitlines() if ln.startswith("# ")), None)
    if h1 is None:
        raise OHMImportError(f"{source}: no '# ' heading (not a charter)")

    flags: list[ImportFlag] = []
    team_num, team_name, subtitle = _parse_header(h1, flags)
    secs = _sections(text)

    purpose = "\n".join(secs.get("purpose", [])).strip()
    if not purpose:
        flags.append(_flag("F-CHARTER-NOPURPOSE", "info", "no ## Purpose section"))

    roster_key = next((k for k in secs if k.startswith("roster")), None)
    roster = _parse_roster(secs[roster_key], flags) if roster_key else []
    if not roster:
        flags.append(_flag("F-CHARTER-NOROSTER", "blocking", "charter has no parseable roster"))

    writes_key = next((k for k in secs if "owns" in k or "writes" in k), None)
    owns_writes = _parse_writes(secs[writes_key], flags) if writes_key else []

    hard_gates = _parse_hard_gates(text, flags)

    handoff_lines: list[str] = []
    for key, body in secs.items():
        if "handoff" in key:
            handoff_lines.extend(body)
    handoffs = _parse_handoffs(handoff_lines, flags)

    return CharterTeam(
        team_num=team_num,
        team_name=team_name,
        subtitle=subtitle,
        purpose=purpose,
        roster=roster,
        owns_writes=owns_writes,
        hard_gates=hard_gates,
        handoffs=handoffs,
        flags=flags,
        source=source,
    )


def parse_charter(path: str | Path) -> CharterTeam:
    """Read and parse a ``teams/<n>/charter.md`` file (fail-closed on read error)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise OHMImportError(f"cannot read charter {p}: {exc}") from exc
    return parse_charter_text(text, source=p.name)
