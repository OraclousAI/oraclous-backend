"""Extract the ``## Handoff`` Next-agent convention from an agent body (ADR-034 §6 depends_on src).

Bitcoin's 17 agents end with a ``## Handoff`` naming ``**Next agent**: <a | b | user-decides>``
and ``**Next task**``. Each named downstream agent is a candidate routing edge (this agent hands to
to it). ``user-decides``/``user`` are dropped (a human branch, not an edge). A multi-candidate or
human-branch handoff is ``conditional``. Pure; the assembler decides DAG-edge vs routing-hint.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_NEXT_AGENT_RE = re.compile(r"\*\*Next agent\*\*\s*:?\s*(.+)", re.IGNORECASE)
_NEXT_TASK_RE = re.compile(r"\*\*Next task\*\*\s*:?\s*(.+)", re.IGNORECASE)
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_NON_AGENT = {"user-decides", "user", "user decides", "none", "end", "done", "n/a", "tbd"}


class HandoffSpec(BaseModel):
    """A parsed ``## Handoff``: the candidate downstream roles + the next-task prompt."""

    model_config = ConfigDict(extra="ignore")

    next_agents: list[str] = Field(default_factory=list)  # named downstream roles (no user-decides)
    next_task: str = ""
    conditional: bool = False  # >1 candidate, or a user-decides branch


def parse_handoff(body: str) -> HandoffSpec:
    """Extract the ``## Handoff`` Next-agent/Next-task convention from an agent body."""
    next_agents: list[str] = []
    conditional = False
    m = _NEXT_AGENT_RE.search(body)
    if m:
        raw = m.group(1).strip().strip("`").strip().lstrip("<").rstrip(">").strip()
        candidates = [c.strip().strip("`").strip() for c in re.split(r"[|/,]", raw) if c.strip()]
        has_human = any(c.lower() in _NON_AGENT for c in candidates)
        next_agents = [c for c in candidates if c.lower() not in _NON_AGENT and _ROLE_RE.match(c)]
        conditional = len(candidates) > 1 or has_human
    tm = _NEXT_TASK_RE.search(body)
    next_task = tm.group(1).strip().strip('"').strip() if tm else ""
    return HandoffSpec(next_agents=next_agents, next_task=next_task, conditional=conditional)
