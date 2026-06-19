"""Map an ``AgentDefinition`` -> an OHM v1.1 ``OHMMember`` + a generated single-agent sub-harness.

ADR-034 §2. The sub-harness is an IMPLICIT single-agent OHM (no actors, no members), so
``runtime.entrypoint`` names a ``capabilities[].binding`` (the load-bearing rule in ``parse.py``).
Every silent default — the provisional model id, the synthesized tool capability ``ref``, the
arbitrary implicit-agent entrypoint, the random manifest id — surfaces as an ``ImportFlag`` for the
O8 dry-run, never resolved silently (ADR-034 §7, flag-not-guess). Pure; fail-closed.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_.parse import AgentDefinition
from oraclous_ohm.manifest import (
    OHMCapability,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMModel,
    OHMPrompt,
    OHMRuntime,
)

# PROVISIONAL — no checked-in model registry exists (confirmed by grounding). Every use raises
# F-MODEL-RESOLVED so the concrete id is surfaced and confirmed before GO; never finalized here.
_MODEL_TIER_BINDINGS: dict[str, str] = {
    "opus": "anthropic/claude-opus-4-8",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "haiku": "anthropic/claude-haiku-4-5",
}

# Conservative in-body human-gate markers (ADR-034 §6, conservative bias). kind:human is #408; here
# we only DETECT and flag, so a missed gate can't silently become an agent member.
_HUMAN_GATE_MARKERS = ("human-or-outsource", "the author uploads", "author uploads", "human gate")

FlagSeverity = Literal["blocking", "confirm", "info"]


class ImportFlag(BaseModel):
    """A surfaced import decision/risk for the O8 dry-run — never a silent resolution."""

    model_config = ConfigDict(extra="ignore")

    code: str
    severity: FlagSeverity
    member_role: str
    message: str


class AgentMapping(BaseModel):
    """Mapping result: the team member, its sub-harness (None if unbuildable), and dry-run flags."""

    model_config = ConfigDict(extra="ignore")

    member: OHMMember
    sub_harness: OHMManifest | None = None
    flags: list[ImportFlag] = Field(default_factory=list)


def slugify(value: str) -> str:
    """Lowercase, non-alphanumeric -> '-', collapsed and trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _dedup_preserving(items: list[str]) -> tuple[list[str], bool]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out, len(out) != len(items)


def map_agent_to_member(
    agent: AgentDefinition, *, owner_organization_id: uuid.UUID
) -> AgentMapping:
    """Map a parsed agent to an ``OHMMember`` + generated sub-harness (ADR-034 §2)."""
    role = slugify(agent.name)
    if not role:
        raise OHMImportError(f"{agent.source}: agent name {agent.name!r} slugifies to empty")

    flags: list[ImportFlag] = []

    def flag(code: str, severity: FlagSeverity, message: str) -> None:
        flags.append(ImportFlag(code=code, severity=severity, member_role=role, message=message))

    if role != agent.name:
        flag("F-SLUG", "confirm", f"role {role!r} differs from source name {agent.name!r}")

    tools, had_dupes = _dedup_preserving(agent.tools)
    if had_dupes:
        flag("F-DUPTOOL", "confirm", "duplicate tools removed; confirm none was meant to differ")

    subgoal = agent.description.strip() or None
    if subgoal is None and agent.body.strip():
        subgoal = next((ln.strip() for ln in agent.body.splitlines() if ln.strip()), None)
        if subgoal:
            flag("F-SUBGOAL-FROMBODY", "info", "subgoal distilled from the body's first line")

    member = OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:{owner_organization_id}/{role}@1",
        tools=tools,
        subgoal=subgoal,
    )

    if any(marker in agent.body.lower() for marker in _HUMAN_GATE_MARKERS):
        flag(
            "F-HUMANGATE",
            "confirm",
            "possible human-gate marker in body; kind:human detection is #408",
        )
    if agent.skills:
        flag("F-SKILLS-DEFERRED", "info", f"{len(agent.skills)} skill(s) not yet resolved (#407)")

    if not tools:
        flag(
            "F-NOTOOLS",
            "blocking",
            "agent declares no tools; sub-harness cannot resolve an entrypoint",
        )
        return AgentMapping(member=member, sub_harness=None, flags=flags)

    capabilities = [OHMCapability(ref=f"core/{slugify(t)}@1", binding=t) for t in tools]
    flag(
        "F-TOOLREF",
        "confirm",
        f"{len(capabilities)} tool ref(s) synthesized core/<name>@1 (no registry)",
    )

    if not agent.model:
        models: list[OHMModel] = []
        flag("F-MODEL-ABSENT", "confirm", "no model declared; runtime default applies")
    else:
        key = agent.model.strip().lower()
        if key in _MODEL_TIER_BINDINGS:
            binding = _MODEL_TIER_BINDINGS[key]
            models = [OHMModel(role="primary", binding=binding, protocol_shape="native")]
            flag("F-MODEL-RESOLVED", "info", f"tier {key!r} -> {binding} (provisional, confirm)")
        else:
            models = [OHMModel(role="primary", binding=agent.model, protocol_shape="native")]
            flag(
                "F-MODEL-PASSTHROUGH",
                "confirm",
                f"model {agent.model!r} not a known tier; verbatim",
            )

    prompts = (
        [OHMPrompt(role="primary", source="inline", body=agent.body)] if agent.body.strip() else []
    )
    if not prompts:
        flag("F-NOPROMPT", "confirm", "agent has no body; sub-harness has no system prompt")

    flag(
        "F-IDGEN",
        "info",
        "sub-harness metadata.id is a random uuid4 (no stable re-import identity)",
    )
    flag(
        "F-ENTRYPOINT",
        "info",
        f"runtime.entrypoint={tools[0]!r} satisfies the load cross-check only",
    )

    sub_harness = OHMManifest(
        ohm_version="1.0",
        metadata=OHMMetadata(
            id=uuid.uuid4(),
            name=role,
            owner_organization_id=owner_organization_id,
            kind="agent",
            description=(agent.description or None),
            labels={"oraclous.import/source": agent.source},
        ),
        capabilities=capabilities,
        models=models,
        prompts=prompts,
        runtime=OHMRuntime(entrypoint=tools[0]),
    )
    return AgentMapping(member=member, sub_harness=sub_harness, flags=flags)
