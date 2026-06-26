"""OHM manifest model (domain layer; OHM v1.0 standalone spec §2).

A pure, I/O-free representation of an Oraclous Harness Manifest. Slice 1 models the structured zone
faithfully but keeps semantics thin: capabilities/models/prompts/runtime are cross-checked
(entrypoint resolves to a declared binding), while atomic reference resolution against the
registry, signature verification, and policy-set governance land in slices 2-3. ``config`` blobs
are passed through opaquely (e.g. a capability's ``credential_mappings`` for the registry instance).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ProtocolShape = Literal["native", "openai-compatible", "gemini-compatible"]


class OHMMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    name: str = Field(min_length=1)
    owner_organization_id: uuid.UUID
    # v1.1: "team" enables the team blocks below; default "agent" keeps v1.0 behaviour.
    kind: Literal["agent", "team"] = "agent"
    created_at: str | None = None
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class OHMCapability(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str = Field(min_length=1)  # core/<name>@<version> | org:<org-id>/<name>@<version>
    binding: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class OHMModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str = Field(min_length=1)
    binding: str = Field(min_length=1)  # <provider>/<model-id>
    protocol_shape: ProtocolShape
    config: dict[str, Any] = Field(default_factory=dict)


class OHMPrompt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str = Field(min_length=1)
    source: Literal["inline", "asset-ref"] = "inline"
    body: str = ""


class OHMGovernance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    policy_set_ref: str | None = None
    rebac_bindings: list[dict[str, Any]] = Field(default_factory=list)
    # regexes redacted from tool results + the final answer before they leave the runtime (Section 6
    # output redaction). A runtime mechanism keyed off the OHM until the taxonomy parametrises it.
    redact_patterns: list[str] = Field(default_factory=list)


class OHMActor(BaseModel):
    """A harness actor (section-4 ``actors[]``). An ``agent`` runs the tool-use loop; a ``human``
    is dispatched as a task-board assignment (R4 halts → escalation; durable resume is R5)."""

    model_config = ConfigDict(extra="ignore")

    role: str = Field(min_length=1)
    kind: Literal["agent", "human"]
    human_role: str | None = None  # for human actors: the workspace role to assign the task to


class OHMRuntime(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entrypoint: str = Field(min_length=1)  # a capability binding (no actors) OR an actor role
    budget: dict[str, Any] = Field(default_factory=dict)
    observability_tags: dict[str, str] = Field(default_factory=dict)


# ── OHM v1.1 team blocks (ADR-031; additive) ───────────────────────────────────────────────
class OHMFanOut(BaseModel):
    """N-way fan-out: one member instance per item in ``over`` (a JSONPath into team state)."""

    model_config = ConfigDict(extra="ignore")

    over: str = Field(min_length=1)
    max_parallel: int = Field(default=1, ge=1)
    # how the fan-out outputs merge (concat | dedupe | group_by | synthesize) — ADR-035 B3.
    # concat/dedupe/group_by are the DETERMINISTIC reducer (aggregate.reduce); synthesize is an
    # LLM-synthesis pass (the member merges its own N outputs into one — e.g. EURail's ledger).
    reduce: str = "concat"
    reduce_field: str | None = None  # extract this list field from each output before merging
    reduce_key: str | None = None  # the dedupe/group_by key
    synthesize_prompt: str | None = (
        None  # the instruction for an LLM synthesis pass (reduce=synthesize)
    )


class OHMRunIf(BaseModel):
    """Declarative conditional dispatch (ADR-035): run this member only if a prior member's output
    satisfies the test — so conditional routing (bitcoin: "research regime is tradeable → dispatch
    the Instrument Design team") is EXPRESSIBLE in the manifest and evaluated by ``run_team``,
    reachable through the team-run API (vs an injected Python predicate). ``from_role`` must be a
    ``depends_on`` of this member so its output is ready; evaluation is fail-closed (skip on any
    error / missing source)."""

    model_config = ConfigDict(extra="ignore")

    from_role: str = Field(min_length=1)  # the prior member whose output to test
    field: str | None = None  # a key into from_role's output dict (None = the whole output)
    op: Literal["truthy", "eq", "ne", "gt", "lt", "gte", "lte", "in"] = "truthy"
    value: Any = None


class OHMMember(BaseModel):
    """A team member (v1.1 ``members[]``; a richer, DAG-capable successor to ``OHMActor``).

    An ``agent`` runs its referenced sub-harness; a ``human`` is a blocking task-board node. The
    ``depends_on`` roles form the fan-in barrier. A ``human`` member requires a ``human_role``.
    """

    model_config = ConfigDict(extra="ignore")

    role: str = Field(min_length=1)
    kind: Literal["agent", "human"]
    manifest_ref: str | None = None  # the sub-harness OHM (kind: agent)
    tools: list[str] = Field(default_factory=list)  # capability ceiling (ADR-032); deny-by-default
    subgoal: str | None = None
    depends_on: list[str] = Field(default_factory=list)  # member roles to wait on (fan-in barrier)
    fan_out: OHMFanOut | None = None
    run_if: OHMRunIf | None = None  # conditional dispatch: skip unless a prior output satisfies it
    inputs: list[str] = Field(default_factory=list)
    outputs_schema: dict[str, Any] = Field(default_factory=dict)  # typed output contract
    human_role: str | None = None  # REQUIRED for kind: human
    schedule: str | None = None  # cron expr for a scheduled standing-team member (ADR-034 §6)

    @model_validator(mode="after")
    def _human_requires_role(self) -> OHMMember:
        if self.kind == "human" and not self.human_role:
            raise ValueError("a human member requires 'human_role'")
        return self


class OHMTermination(BaseModel):
    """Goal-aware stop conditions for a team run (distinct from per-member tool/time caps)."""

    model_config = ConfigDict(extra="ignore")

    max_wall_seconds: int | None = None
    max_rounds: int | None = None
    convergence: str | None = None  # e.g. "evaluator>=0.8"


class OHMGateCheck(BaseModel):
    """One check in a named gate battery (ADR-037 Decision 2 / #470). ``evaluator`` checks grade
    ``rubric`` via ``core/evaluate``; ``deterministic`` checks call a registered ``core/check/<id>``
    predicate by ``check_ref``. ``severity`` is the precedence/AND-floor tier (default CRITICAL =
    blocking). ``applies_when`` (REUSE ``OHMRunIf``) skips refresh-only gates fail-closed."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)  # unique within the battery; the addressable check id
    kind: Literal["evaluator", "deterministic"]
    rubric: str | None = None  # evaluator: the prose criterion the judge grades
    check_ref: str | None = None  # deterministic: a registered core/check/<id> predicate
    params: dict[str, Any] = Field(
        default_factory=dict
    )  # e.g. {"min_ratio": 0.333, "threshold": 0.6}
    severity: Literal["CRITICAL", "MAJOR", "MINOR"] = "CRITICAL"
    applies_when: OHMRunIf | None = None

    @model_validator(mode="after")
    def _kind_requires_its_target(self) -> OHMGateCheck:
        # fail-fast (#479): an evaluator check MUST carry a non-empty rubric (else it grades the
        # empty string → core/evaluate 422s on min_length and collapses the whole battery); a
        # deterministic check MUST name a check_ref. Catch the misdeclaration at load, not at grade.
        if self.kind == "evaluator" and not (self.rubric and self.rubric.strip()):
            raise ValueError("an evaluator gate check requires a non-empty 'rubric'")
        if self.kind == "deterministic" and not self.check_ref:
            raise ValueError("a deterministic gate check requires a 'check_ref'")
        return self


class OHMGateBattery(BaseModel):
    """A named, deterministic, multi-check evaluator battery (ADR-037 Decision 2 / #470).

    ``floor: "and"`` (EURail report-editor 10-gate) PASSES iff every applicable check passes — flat,
    no tiers. ``floor: "precedence"`` (book QA Lock) PASSES iff no CRITICAL check fails; MAJOR/MINOR
    failures are reported-but-non-blocking while every CRITICAL clears (integrity > fact > grammar >
    engagement). Checks are ordered = evaluation order + within-tier precedence rank."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    description: str = ""
    checks: list[OHMGateCheck]
    floor: Literal["and", "precedence"] = "and"


class OHMLoop(BaseModel):
    """A strongly-connected component of the handoff graph — a GENUINE cycle (ADR-043 #552) the
    conductor runs as a bounded coordinator seam, while the acyclic remainder runs on ``run_team``.

    ``members`` are the SCC's roles (the loop); ``routing`` preserves each member's ``## Handoff``
    next_task — the per-edge routing intent the bounded coordinator uses to re-dispatch the next
    member with a concrete objective. The intra-loop edges are NOT ``depends_on`` (they would make
    the member DAG cyclic); the loop carries them so the skeleton stays acyclic."""

    model_config = ConfigDict(extra="ignore")

    members: list[str] = Field(min_length=1)
    routing: dict[str, str] = Field(default_factory=dict)  # role -> its ## Handoff next_task


class OHMOrchestration(BaseModel):
    """The coordinator's brief — routing CHOICE is prose; mechanics/budgets/gates stay coded."""

    model_config = ConfigDict(extra="ignore")

    # media: round-table | board | blackboard | handoff | a2a
    medium: list[str] = Field(default_factory=list)
    style: str = ""
    success_criteria: str = ""
    termination: OHMTermination = Field(default_factory=OHMTermination)
    # ADR-043 #552: the genuine loops (Tarjan SCCs) isolated at import — each runs the conductor's
    # bounded coordinator seam; an empty list means a purely acyclic team (runs all on run_team).
    loops: list[OHMLoop] = Field(default_factory=list)


class OHMTaskBoard(BaseModel):
    """First-class assignable tasks for the team."""

    model_config = ConfigDict(extra="ignore")

    columns: list[str] = Field(
        default_factory=lambda: [
            "proposed",
            "claimed",
            "in_progress",
            "blocked",
            "done",
            "escalated",
        ]
    )


class OHMBudget(BaseModel):
    """The TEAM-POOLED budget — the single governed ceiling for the whole fan-out (ADR-031 keystone:
    one Team Harness = one budget surface; no per-member budget escapes it)."""

    model_config = ConfigDict(extra="ignore")

    max_tokens_total: int | None = None
    max_tool_calls_total: int | None = None
    max_sub_runs: int | None = None
    max_usd_total: float | None = None
    ttl_seconds: int | None = None


class OHMPrecedence(BaseModel):
    """Hierarchy-of-Truth (A-NEW-3): the source's truth ranking (highest first). ``graph: derived``
    (default) keeps graph state derived-and-disposable; ``authoritative`` is an explicit opt-in mode
    — graph-as-truth is never imposed."""

    model_config = ConfigDict(extra="ignore")

    order: list[str] = Field(default_factory=list)
    graph: Literal["authoritative", "derived"] = "derived"


class OHMManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ohm_version: str
    metadata: OHMMetadata
    capabilities: list[OHMCapability] = Field(default_factory=list)
    models: list[OHMModel] = Field(default_factory=list)
    prompts: list[OHMPrompt] = Field(default_factory=list)
    actors: list[OHMActor] = Field(default_factory=list)
    governance: OHMGovernance = Field(default_factory=OHMGovernance)
    runtime: OHMRuntime
    signatures: list[dict[str, Any]] = Field(default_factory=list)

    # ── v1.1 team blocks (additive; consulted only when metadata.kind == "team") ────────────
    members: list[OHMMember] = Field(default_factory=list)
    orchestration: OHMOrchestration | None = None
    task_board: OHMTaskBoard | None = None
    budget: OHMBudget | None = None
    precedence: OHMPrecedence | None = None
    schemas: dict[str, Any] = Field(default_factory=dict)
    # ADR-037 / E4 #470 — named gate batteries (a sibling of `schemas`; same record-once rule).
    # Referenced from orchestration.success_criteria / termination.convergence by a `battery:<name>`
    # token; resolved fail-closed (an undeclared reference aborts the load — see `resolve_battery`).
    batteries: dict[str, OHMGateBattery] = Field(default_factory=dict)

    # ── resolution helpers (pure) ──────────────────────────────────────────────
    def is_team(self) -> bool:
        """True when this manifest is a Team Harness (OHM v1.1)."""
        return self.metadata.kind == "team"

    def battery_by_name(self, name: str) -> OHMGateBattery | None:
        return self.batteries.get(name)

    def member_by_role(self, role: str) -> OHMMember | None:
        return next((m for m in self.members if m.role == role), None)

    def execution_stages(self) -> list[list[str]]:
        """Topologically-ordered execution stages over the team's ``members`` — fan-out within a
        stage, fan-in barrier between stages. Empty when the manifest has no members. Raises
        ``OHMDagError`` on a cycle / unknown depends_on / duplicate role (fail-closed)."""
        from oraclous_ohm.dag import topological_stages

        return topological_stages(self.members)

    def capability_by_binding(self, binding: str) -> OHMCapability | None:
        return next((c for c in self.capabilities if c.binding == binding), None)

    def entrypoint_capability(self) -> OHMCapability | None:
        return self.capability_by_binding(self.runtime.entrypoint)

    def actor_by_role(self, role: str) -> OHMActor | None:
        return next((a for a in self.actors if a.role == role), None)

    def entrypoint_actor(self) -> OHMActor | None:
        """The actor the run starts with (None when the OHM declares no actors — implicit agent)."""
        return self.actor_by_role(self.runtime.entrypoint)

    def model_by_role(self, role: str) -> OHMModel | None:
        return next((m for m in self.models if m.role == role), None)

    def primary_model(self) -> OHMModel | None:
        return self.model_by_role("primary") or (self.models[0] if self.models else None)

    def evaluator_model(self) -> OHMModel | None:
        """The team's BYOM judge model for flow-evaluation (ADR-037 / BYOM-judge): a ``models[]``
        entry with ``role="evaluator"`` carrying ``config.credential_id``. Unlike ``primary_model``
        there is NO ``models[0]`` fallback — absence returns ``None`` so the gate falls back to the
        operator-configured judge key rather than mis-grading with the first model's credential."""
        return self.model_by_role("evaluator")

    def prompt_by_role(self, role: str) -> OHMPrompt | None:
        return next((p for p in self.prompts if p.role == role), None)

    def primary_prompt(self) -> OHMPrompt | None:
        return self.prompt_by_role("primary") or (self.prompts[0] if self.prompts else None)
