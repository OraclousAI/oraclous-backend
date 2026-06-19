"""OHM manifest model (ORAA-4 §21 domain layer; OHM v1.0 standalone spec §2).

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


class OHMMember(BaseModel):
    """A team member (v1.1 ``members[]``; a richer, DAG-capable successor to ``OHMActor``).

    An ``agent`` runs its referenced sub-harness; a ``human`` is a blocking task-board node. The
    ``depends_on`` roles form the fan-in barrier. A ``human`` member requires a ``human_role``.
    """

    model_config = ConfigDict(extra="ignore")

    role: str = Field(min_length=1)
    kind: Literal["agent", "human"]
    manifest_ref: str | None = None  # the sub-harness OHM (kind: agent)
    subgoal: str | None = None
    depends_on: list[str] = Field(default_factory=list)  # member roles to wait on (fan-in barrier)
    fan_out: OHMFanOut | None = None
    inputs: list[str] = Field(default_factory=list)
    outputs_schema: dict[str, Any] = Field(default_factory=dict)  # typed output contract
    human_role: str | None = None  # REQUIRED for kind: human

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


class OHMOrchestration(BaseModel):
    """The coordinator's brief — routing CHOICE is prose; mechanics/budgets/gates stay coded."""

    model_config = ConfigDict(extra="ignore")

    # media: round-table | board | blackboard | handoff | a2a
    medium: list[str] = Field(default_factory=list)
    style: str = ""
    success_criteria: str = ""
    termination: OHMTermination = Field(default_factory=OHMTermination)


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

    # ── resolution helpers (pure) ──────────────────────────────────────────────
    def is_team(self) -> bool:
        """True when this manifest is a Team Harness (OHM v1.1)."""
        return self.metadata.kind == "team"

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

    def prompt_by_role(self, role: str) -> OHMPrompt | None:
        return next((p for p in self.prompts if p.role == role), None)

    def primary_prompt(self) -> OHMPrompt | None:
        return self.prompt_by_role("primary") or (self.prompts[0] if self.prompts else None)
