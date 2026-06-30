"""#596 (ADR-047 §5) — the SEED DEFAULTS the compiler plans against, + bootstrap / diff-accept.

A from-scratch org has nothing to survey: the surveyor draws the drafter's allowed ``member.tools``
from the live registry PLUS this seed CAPABILITY INVENTORY (member archetypes + tool groups), the
drafter emits the seed POLICY TEMPLATE so a compiled team is governed-by-default, and the planner
composes from the seed REFERENCE TOPOLOGY shapes. Without these a fresh org's survey is empty and
every sub-goal fails closed (capability-absence, ADR-032). These are declarative data + PURE
resolvers in ``oraclous_ohm`` (NOT a service — ADR-047 Alt G rejected); BOOTSTRAP/DIFF-ACCEPT is a
pure function over (existing-org-seed, new-default-set) returning a diff + a merged set, mirroring
the importer's report-not-clobber shape (``ImportReport`` / ``would_block`` / ``render_report``).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.manifest import OHMBudget, OHMFanOut, OHMGovernance, OHMMember

# The seed policy-set ref MUST equal harness-runtime ``policy.py``'s ``DEFAULT_POLICY_SET_REF`` so
# ``resolve_policy_set`` never fail-closes on an unknown ref (ohm cannot import the service; this
# string is the contract). A drafted team carrying this ref resolves on the real stack.
DEFAULT_POLICY_SET_REF = "policy-set:development-default@1.0.0"

# The seed default per-run team pool (L2) + the per-agent safety sub-ceilings (L1, each <= the
# pool — the resolve_member_caps clamp invariant). NO 6-field per-member OHMMemberBudget block
# (ADR-031's rejected Alternative C); nothing here precludes a future L3 period window (ADR-048/E8).
_POOL_TOKENS = 500_000
_POOL_TOOL_CALLS = 200
_PER_MEMBER_TOKENS = 100_000  # <= _POOL_TOKENS
_PER_MEMBER_TOOL_CALLS = 50  # <= _POOL_TOOL_CALLS


class MemberArchetype(BaseModel):
    """A reusable member shape the surveyor offers + the planner composes from: a role + a curated
    ``tools`` set drawn from REAL registered capabilities (no phantom tool)."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)
    role_description: str = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)  # bare slugs of real registered capabilities


class ToolGroup(BaseModel):
    """A named bundle of registry tool slugs the surveyor can offer as a unit."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)


class ReferenceTopology(BaseModel):
    """A reference ``members[]`` SHAPE the planner composes from — never a frozen pipeline (ADR-047
    Alternative C rejected). Each shape is an ACYCLIC OHM v1.1 fragment."""

    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)
    description: str = ""
    members: list[OHMMember]


class CapabilityInventory(BaseModel):
    model_config = ConfigDict(extra="ignore")
    archetypes: list[MemberArchetype] = Field(default_factory=list)
    tool_groups: list[ToolGroup] = Field(default_factory=list)


class PolicyTemplate(BaseModel):
    """The governance + budget envelope the drafter emits on EVERY compiled team."""

    model_config = ConfigDict(extra="ignore")
    governance: OHMGovernance
    budget: OHMBudget


class SeedSet(BaseModel):
    """The whole seed surface bootstrapped into an org."""

    model_config = ConfigDict(extra="ignore")
    inventory: CapabilityInventory
    policy: PolicyTemplate
    topologies: list[ReferenceTopology] = Field(default_factory=list)


# --------------------------------------------------------------------------
# The default seed batteries (ADR-047 §5). Every ``tools`` slug below is a REAL capability
# (capability-registry builtin.py) so a survey that merges these never offers a phantom tool.
# --------------------------------------------------------------------------


def seed_capability_inventory() -> CapabilityInventory:
    return CapabilityInventory(
        archetypes=[
            MemberArchetype(
                name="researcher",
                role_description="Research a topic from the live web and the org's knowledge.",
                tools=["web-research", "websearch", "webfetch"],
            ),
            MemberArchetype(
                name="fact-checker",
                role_description="Verify the claims in a draft against sources.",
                tools=["web-research", "knowledge-retriever"],
            ),
            MemberArchetype(
                name="writer",
                role_description="Draft and revise written content from the inputs.",
                tools=["write", "text-tools"],
            ),
            MemberArchetype(
                name="editor",
                role_description="Review and edit a draft for quality and correctness.",
                tools=["read", "edit"],
            ),
            MemberArchetype(
                name="analyst",
                role_description="Fetch and analyse structured data from curated sources.",
                tools=["rest-connector", "text-tools"],
            ),
            MemberArchetype(
                name="knowledge-curator",
                role_description="Retrieve, relate, and recall the org's knowledge graph + memory.",
                tools=["knowledge-retriever", "find-similar", "recall-memory"],
            ),
            MemberArchetype(
                name="publisher",
                role_description="Deliver the finished artifact to the user's drafts.",
                tools=["send-to-drafts"],
            ),
        ],
        tool_groups=[
            ToolGroup(name="research", tools=["web-research", "websearch", "webfetch"]),
            ToolGroup(name="filesystem", tools=["read", "write", "edit", "grep", "glob"]),
            ToolGroup(
                name="knowledge", tools=["knowledge-retriever", "find-similar", "recall-memory"]
            ),
            ToolGroup(name="delivery", tools=["send-to-drafts"]),
        ],
    )


def seed_policy_template() -> PolicyTemplate:
    """The governed-by-default envelope: a KNOWN policy_set_ref + bounded redact_patterns + the
    re-baselined 3-layer ADR-044 budget (L2 pool + L1 per-agent caps each <= the pool; NO
    OHMMemberBudget; nothing precluding a future L3 window)."""
    return PolicyTemplate(
        governance=OHMGovernance(
            policy_set_ref=DEFAULT_POLICY_SET_REF,
            redact_patterns=[
                # BOUNDED quantifiers (no unbounded ``+``) so the redact pass over attacker-shaped
                # tool output can't catastrophically backtrack (ReDoS); the limits fit real values.
                r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}\b",  # email
                r"\b(?:\d[ -]?){13,16}\b",  # card-like number (bounded, single optional separator)
            ],
        ),
        budget=OHMBudget(
            max_tokens_total=_POOL_TOKENS,
            max_tool_calls_total=_POOL_TOOL_CALLS,
            max_sub_runs=20,
            max_tokens_per_member=_PER_MEMBER_TOKENS,
            max_tool_calls_per_member=_PER_MEMBER_TOOL_CALLS,
        ),
    )


def seed_reference_topologies() -> list[ReferenceTopology]:
    """≥3 acyclic reference shapes the planner composes from (fan-out/fan-in, standing-team,
    gated-pipeline). Each ``members[]`` is a valid OHM fragment with acyclic ``depends_on``."""
    return [
        ReferenceTopology(
            name="fan-out-fan-in",
            description="A worker fanned out over a list, synthesised by a reducer.",
            members=[
                OHMMember(
                    role="worker",
                    kind="agent",
                    manifest_ref="org:ref/worker@1",
                    fan_out=OHMFanOut(over="$.items", max_parallel=4, reduce="synthesize"),
                ),
                OHMMember(
                    role="synthesizer",
                    kind="agent",
                    manifest_ref="org:ref/synth@1",
                    depends_on=["worker"],
                ),
            ],
        ),
        ReferenceTopology(
            name="standing-team",
            description="A scheduled monitor feeding a reporter — a persistent standing team.",
            members=[
                OHMMember(
                    role="monitor",
                    kind="agent",
                    manifest_ref="org:ref/monitor@1",
                    schedule="0 * * * *",
                ),
                OHMMember(
                    role="reporter",
                    kind="agent",
                    manifest_ref="org:ref/reporter@1",
                    depends_on=["monitor"],
                ),
            ],
        ),
        ReferenceTopology(
            name="gated-pipeline",
            description="A linear draft → review chain ending in a reviewer gate.",
            members=[
                OHMMember(role="drafter", kind="agent", manifest_ref="org:ref/drafter@1"),
                OHMMember(
                    role="reviewer",
                    kind="agent",
                    manifest_ref="org:ref/reviewer@1",
                    depends_on=["drafter"],
                ),
            ],
        ),
    ]


def default_seed_set() -> SeedSet:
    """The full default seed surface a fresh org bootstraps against (ADR-047 §5)."""
    return SeedSet(
        inventory=seed_capability_inventory(),
        policy=seed_policy_template(),
        topologies=seed_reference_topologies(),
    )


def survey_catalog(inventory: CapabilityInventory, registered: list[str]) -> list[str]:
    """The surveyor's catalog: the seed inventory's tools UNION the LIVE registry's slugs — so a
    fresh org (whose live survey would be sparse) still offers a non-empty, real-tool catalog. The
    returned slugs are de-duplicated; every seed tool is a real registered capability."""
    seed = {t for a in inventory.archetypes for t in a.tools}
    seed |= {t for g in inventory.tool_groups for t in g.tools}
    live = {_slug(r) for r in registered}
    return sorted(seed | live)


def _basic_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _slug(ref: str) -> str:
    """Normalise a registry NAME or ref to its bare slug — ALIGNED with
    ``compiler.validate._tool_slug`` (#594 hardening; kept inline, NOT imported, to avoid a
    compiler↔seeds import cycle). Strip a trailing ``@version`` and ONLY a leading canonical
    ``core/`` namespace; a remaining ``/`` marks a FOREIGN namespace and is encoded ``ns--…`` (an
    empty segment → ``""``), so ``evil/web-research`` can NEVER collapse to the bare surveyed
    ``web-research`` (the exact masquerade #594 closed).
    ``'Web Research'`` / ``'core/web-research@1'`` both → ``web-research``."""
    s = ref.strip().lower().split("@", 1)[0]
    if s.startswith("core/"):
        s = s[len("core/") :]
    if "/" in s:
        parts = [_basic_slug(seg) for seg in s.split("/")]
        return "ns--" + "--".join(parts) if all(parts) else ""
    return _basic_slug(s)


# --------------------------------------------------------------------------
# Bootstrap / diff-accept (ADR-047 §5 item 4) — report-not-clobber, mirroring ImportReport.
# --------------------------------------------------------------------------


class SeedDiff(BaseModel):
    """A re-seed diff (mirrors ``ImportReport``): what a newer default set ADDED, what CHANGED, and
    which seeds the USER edited (never silently overwritten). ``would_block`` is False — a re-seed
    never blocks; it just declines to clobber user edits."""

    model_config = ConfigDict(extra="ignore")
    added: list[str] = Field(default_factory=list)  # new seed names applied
    # RESERVED: populated only by a future 3-way merge (with a stored last-applied baseline) that
    # can tell a default bump from a user edit. The current 2-way merge has no baseline, so it
    # CONSERVATIVELY classifies any present-but-different seed as ``user_modified`` (never-clobber);
    # ``changed`` stays empty. A default bump on an unedited org needs a deliberate re-accept.
    changed: list[str] = Field(default_factory=list)
    user_modified: list[str] = Field(default_factory=list)  # user-edited → kept, NOT overwritten


def bootstrap_seed(existing: SeedSet | None, new_default: SeedSet) -> tuple[SeedSet, SeedDiff]:
    """First-run bootstrap (``existing is None`` → the full default set) OR a diff-accept re-seed.
    Applies only NON-conflicting ADDITIONS (a brand-new default seed); a present-but-different seed
    is KEPT, flagged ``user_modified``, never overwritten (the 2-way merge has no baseline to tell a
    default bump from a user edit — see ``SeedDiff.changed``). Re-bootstrapping the same set is a
    no-op (idempotent)."""
    if existing is None:
        names = _seed_names(new_default)
        return new_default, SeedDiff(added=names)

    merged_inv, inv_diff = _merge_named(
        existing.inventory.archetypes, new_default.inventory.archetypes
    )
    merged_groups, grp_diff = _merge_named(
        existing.inventory.tool_groups, new_default.inventory.tool_groups
    )
    merged_topos, topo_diff = _merge_named(existing.topologies, new_default.topologies)

    # the policy template is a single record: apply a newer default only if unedited
    policy = existing.policy
    diff = SeedDiff()
    for d in (inv_diff, grp_diff, topo_diff):
        diff.added += d.added
        diff.changed += d.changed
        diff.user_modified += d.user_modified
    if existing.policy != new_default.policy:
        # a default-vs-existing policy difference: the user edited it (we cannot tell a default bump
        # from a user edit on a singleton) → keep the user's, flag it, never clobber.
        diff.user_modified.append("policy-template")

    merged = SeedSet(
        inventory=CapabilityInventory(archetypes=merged_inv, tool_groups=merged_groups),
        policy=policy,
        topologies=merged_topos,
    )
    return merged, diff


def _seed_names(s: SeedSet) -> list[str]:
    return (
        [a.name for a in s.inventory.archetypes]
        + [g.name for g in s.inventory.tool_groups]
        + [t.name for t in s.topologies]
        + ["policy-template"]
    )


def _merge_named(existing: list[Any], new: list[Any]) -> tuple[list[Any], SeedDiff]:
    """Merge two name-keyed seed lists: keep every existing entry (never clobber a user edit), ADD
    entries only the default has. A name present in both but DIFFERENT is a user edit → flagged
    ``user_modified``, NEVER overwritten."""
    by_name = {e.name: e for e in existing}
    diff = SeedDiff()
    merged = list(existing)
    for n in new:
        if n.name not in by_name:
            merged.append(n)
            diff.added.append(n.name)
        elif by_name[n.name] != n:
            diff.user_modified.append(n.name)  # differs → assume a user edit; keep theirs
    return merged, diff
