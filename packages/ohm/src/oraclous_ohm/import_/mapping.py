"""Map an ``AgentDefinition`` -> an OHM v1.1 ``OHMMember`` + a generated single-agent sub-harness.

ADR-034 §2. The sub-harness is an IMPLICIT single-agent OHM (no actors, no members), so
``runtime.entrypoint`` names a ``capabilities[].binding`` (the load-bearing rule in ``parse.py``).
Every silent default — the provisional model id, the synthesized tool capability ``ref``, the
arbitrary implicit-agent entrypoint, the random manifest id — surfaces as an ``ImportFlag`` for the
O8 dry-run, never resolved silently (ADR-034 §7, flag-not-guess). Pure; fail-closed.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm._slug import (
    FILE_SUBSTRATE_WRITE_TOOLS,
    GRAPH_READ_TOOLS,
    GRAPH_WRITE_TOOLS,
    basic_slug,
    tool_slug,
)
from oraclous_ohm.errors import OHMImportError
from oraclous_ohm.import_._flags import FlagSeverity, ImportFlag
from oraclous_ohm.import_.parse import AgentDefinition
from oraclous_ohm.import_.skills import ResolvedSkill, inline_skills, try_resolve_skill
from oraclous_ohm.manifest import (
    OHMActor,
    OHMCapability,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMModel,
    OHMPrompt,
    OHMRuntime,
    OHMSkillDriver,
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

Substrate = Literal["graph", "file"]

# Cloud-first / graph-primary (#509, ADR-040 Decision 7): under the default graph substrate an
# imported team's declared FILE tools are remapped onto the seeded GRAPH capabilities — members
# RETRIEVE FROM / WRITE TO the graph, not a server-side file sandbox. Only the capability **ref**
# changes (the registry resolves by slug, dropping @version); the binding (the tool name) is
# preserved, so the member's tools ceiling (ADR-032, binding-based) stays valid and the model keeps
# calling Read/Write — they just hit graph retrieval / ingest. Bash stays the sandbox exec fallback
# (#507); a non-file tool keeps its synthesized core/<slug>@1 ref. ``substrate="file"`` is the
# explicit opt-out for the parked local-single-tenant mode (#512/#518), kept as-is.
# The table is keyed on the SHARED tool slug (#694), not on a raw tool name. It used to be keyed
# on the Claude-Code names, so it fired for the importer (whose tools arrive as ``Write``) and
# missed for the compiler (whose tools arrive as the lower-cased catalog slug ``write``). Every
# member of team run ``fe548aac`` fell through to ``core/write@1`` and wrote ~10 KB of deliverables
# into ``/tmp/oraclous-agent-sandbox/<org>/`` while its bound graph stayed empty.
#
# The graph tools name THEMSELVES here too, at their seeded refs. ``graph-ingest`` joins the seed
# inventory in this slice, so the drafter now picks it directly; without these rows a member naming
# it would take the provisional ``core/graph-ingest@1`` while a member declaring ``write`` took the
# seeded ``core/graph-ingest@1.0.0`` — two refs for one capability, which is #694's own drift
# reintroduced by its fix.
_SEEDED_GRAPH_REFS: dict[str, str] = {
    "graph-ingest": "core/graph-ingest@1.0.0",
    "knowledge-retriever": "core/knowledge-retriever@1.0.0",
    "find-similar": "core/find-similar@1.0.0",
    "recall-memory": "core/recall-memory@1.0.0",
}

_GRAPH_REMAP: dict[str, str] = {
    # the file WRITE side, whichever of it exists, lands on the one graph write capability
    **dict.fromkeys(FILE_SUBSTRATE_WRITE_TOOLS, _SEEDED_GRAPH_REFS["graph-ingest"]),
    # the file READ side splits: a content read is retrieval, a name/pattern read is similarity
    "read": _SEEDED_GRAPH_REFS["knowledge-retriever"],
    "grep": _SEEDED_GRAPH_REFS["knowledge-retriever"],
    "glob": _SEEDED_GRAPH_REFS["find-similar"],
    # ...and the graph tools name themselves, at those same refs
    **{t: _SEEDED_GRAPH_REFS[t] for t in GRAPH_WRITE_TOOLS | GRAPH_READ_TOOLS},
}


def _capability_ref(tool: str, substrate: Substrate) -> str:
    """The sub-harness capability ref for a declared tool. Under the graph substrate a file tool
    remaps onto its seeded graph capability (real ref, not provisional); otherwise the tool's ref is
    the provisional synthesized ``core/<slug>@1`` (surfaced as F-TOOLREF for the O8 dry-run).

    The lookup normalises through the ONE shared slug function, so ``Write`` and ``write`` are the
    same tool to it — the case-sensitivity that let a compiled member escape the remap (#694)."""
    if substrate == "graph":
        remapped = _GRAPH_REMAP.get(tool_slug(tool))
        if remapped is not None:
            return remapped
    return f"core/{slugify(tool)}@1"


def remap_capability_refs(descriptor: dict, substrate: Substrate = "graph") -> dict:
    """Return ``descriptor`` with every ``capabilities[].ref`` put on the right substrate (#694).

    ``build_subharness`` already does this for a sub-harness the platform SYNTHESIZES, keyed on the
    declared tool name. This does it for one that ARRIVES built — an import, a draft compiled before
    the fix, or a caller-supplied ``sub_harnesses`` entry — and it keys on the REF, because that is
    what a descriptor actually carries and what the harness actually dispatches.

    Keying on the ref rather than the binding is the load-bearing part. The binding is the member's
    ceiling (ADR-032) and a supplied descriptor can hold ``{"ref": "core/write@1", "binding":
    "graph-ingest"}`` — a per-organisation tmp sandbox behind a clean, ceiling-passing name. The
    binding is preserved untouched, so the ceiling stays valid; only the ref moves.

    A ref that is not on the file substrate is returned verbatim, so this is a no-op for a
    descriptor that was already correct. Pure: the input is not mutated.
    """
    if substrate != "graph":
        return descriptor
    capabilities = descriptor.get("capabilities")
    if not isinstance(capabilities, list):
        return descriptor
    remapped: list[object] = []
    changed = False
    for cap in capabilities:
        ref = cap.get("ref") if isinstance(cap, dict) else None
        target = _GRAPH_REMAP.get(tool_slug(ref)) if isinstance(ref, str) else None
        if target is None or target == ref:
            remapped.append(cap)
            continue
        remapped.append({**cap, "ref": target})
        changed = True
    return {**descriptor, "capabilities": remapped} if changed else descriptor


class AgentMapping(BaseModel):
    """Mapping result: the team member, its sub-harness (None if unbuildable), and dry-run flags."""

    model_config = ConfigDict(extra="ignore")

    member: OHMMember
    sub_harness: OHMManifest | None = None
    flags: list[ImportFlag] = Field(default_factory=list)


slugify = basic_slug


def _dedup_preserving(items: list[str]) -> tuple[list[str], bool]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out, len(out) != len(items)


def build_subharness(
    role: str,
    *,
    owner_organization_id: uuid.UUID,
    body: str,
    tools: list[str] | None = None,
    model: OHMModel | None = None,
    description: str | None = None,
    source: str = "<import>",
    substrate: Substrate = "graph",
    driver: OHMSkillDriver | None = None,
) -> OHMManifest:
    """Build a loadable sub-harness via the actors path (entrypoint -> a 'primary' agent actor).

    A tool-less agent is valid (reasoning-only) — the actor entrypoint loads without capabilities,
    so this never leans on an arbitrary tool to satisfy the loader. ``tools`` populate the sub-
    harness ``capabilities[]``; the parent member's ``tools`` ceiling is set separately. Under the
    default graph ``substrate`` file tools remap onto the seeded graph capabilities (#509).
    """
    capabilities = [
        OHMCapability(ref=_capability_ref(t, substrate), binding=t) for t in (tools or [])
    ]
    prompts = [OHMPrompt(role="primary", source="inline", body=body)] if body.strip() else []
    return OHMManifest(
        ohm_version="1.0",
        metadata=OHMMetadata(
            id=uuid.uuid4(),
            name=role,
            owner_organization_id=owner_organization_id,
            kind="agent",
            description=description,
            labels={"oraclous.import/source": source},
        ),
        capabilities=capabilities,
        models=[model] if model else [],
        prompts=prompts,
        actors=[OHMActor(role="primary", kind="agent")],
        runtime=OHMRuntime(entrypoint="primary", driver=driver),  # #577 slice-3: staged CLI driver
    )


def map_agent_to_member(
    agent: AgentDefinition,
    *,
    owner_organization_id: uuid.UUID,
    skills_root: str | Path | None = None,
    substrate: Substrate = "graph",
) -> AgentMapping:
    """Map a parsed agent to an ``OHMMember`` + generated sub-harness (ADR-034 §2).

    When ``skills_root`` is given, the agent's ``skills`` are resolved there (#406): leaf skills are
    inlined into the sub-harness prompt; orchestrator/missing skills are flagged, not inlined.
    """
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
    if agent.skills and skills_root is None:
        flag(
            "F-SKILLS-DEFERRED",
            "info",
            f"{len(agent.skills)} skill(s) unresolved; pass skills_root",
        )

    if not tools:
        flag("F-NOTOOLS", "info", "agent declares no tools; reasoning-only sub-harness")
    else:
        # under the graph substrate (#509) the file tools remap onto REAL seeded graph caps — only
        # the still-provisional synthesized core/<slug>@1 refs warrant F-TOOLREF; a remapped tool is
        # surfaced as F-GRAPHTOOL so the O8 dry-run shows the substrate decision (flag-not-guess).
        remapped = [t for t in tools if _capability_ref(t, substrate) != f"core/{slugify(t)}@1"]
        synthesized = [t for t in tools if t not in remapped]
        if synthesized:
            flag(
                "F-TOOLREF",
                "confirm",
                f"{len(synthesized)} tool ref(s) synthesized core/<name>@1 (no registry)",
            )
        if remapped:
            flag(
                "F-GRAPHTOOL",
                "info",
                f"{len(remapped)} file tool(s) remapped onto seeded graph capabilities (#509)",
            )

    model: OHMModel | None = None
    if not agent.model:
        flag("F-MODEL-ABSENT", "confirm", "no model declared; runtime default applies")
    else:
        key = agent.model.strip().lower()
        if key in _MODEL_TIER_BINDINGS:
            binding = _MODEL_TIER_BINDINGS[key]
            model = OHMModel(role="primary", binding=binding, protocol_shape="native")
            flag("F-MODEL-RESOLVED", "info", f"tier {key!r} -> {binding} (provisional, confirm)")
        else:
            model = OHMModel(role="primary", binding=agent.model, protocol_shape="native")
            flag(
                "F-MODEL-PASSTHROUGH",
                "confirm",
                f"model {agent.model!r} not a known tier; verbatim",
            )

    # resolve + inline leaf skills (#406); orchestrator/missing skills are flagged, never inlined.
    # #577 slice-3: a driver skill (an external CLI package) is STAGED on the sub-harness runtime,
    # never inlined as prose — at most one per agent.
    leaves: list[ResolvedSkill] = []
    driver: OHMSkillDriver | None = None
    if skills_root is not None:
        for skill in agent.skills:
            try:
                resolved = try_resolve_skill(skill, skills_root)
            except OHMImportError as exc:
                flag("F-SKILL-MISSING", "blocking", f"skill {skill!r} malformed: {exc}")
                continue
            if resolved is None:
                flag("F-SKILL-MISSING", "blocking", f"skill {skill!r} not found under skills_root")
            elif resolved.kind == "driver" and resolved.driver is not None:
                if driver is not None:
                    flag(
                        "F-SKILL-DRIVER-MULTIPLE",
                        "blocking",
                        f"agent has >1 driver; {skill!r} dropped",
                    )
                else:
                    driver = resolved.driver
                    flag(
                        "F-SKILL-DRIVER",
                        "confirm",
                        f"skill {skill!r} is a {driver.kind} driver (entry {driver.entry_point}); "
                        f"staged on runtime.driver, not inlined",
                    )
            elif resolved.kind == "orchestrator":
                sig = ", ".join(resolved.orchestrator_signals)
                flag(
                    "F-SKILL-ORCHESTRATOR", "confirm", f"skill {skill!r} orchestrator ({sig}); #407"
                )
            else:
                leaves.append(resolved)
                flag(
                    "F-SKILL-RESOLVED",
                    "info",
                    f"skill {skill!r} (leaf) inlined from {resolved.source}",
                )

    effective_body = inline_skills(agent.body, leaves)
    if not effective_body.strip():
        flag("F-NOPROMPT", "confirm", "agent has no body; sub-harness has no system prompt")

    flag(
        "F-IDGEN",
        "info",
        "sub-harness metadata.id is a random uuid4 (no stable re-import identity)",
    )

    sub_harness = build_subharness(
        role,
        owner_organization_id=owner_organization_id,
        body=effective_body,
        tools=tools,
        model=model,
        driver=driver,
        description=(agent.description or None),
        source=agent.source,
        substrate=substrate,
    )
    return AgentMapping(member=member, sub_harness=sub_harness, flags=flags)
