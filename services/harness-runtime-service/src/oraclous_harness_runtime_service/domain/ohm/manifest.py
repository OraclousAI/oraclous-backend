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

from pydantic import BaseModel, ConfigDict, Field

ProtocolShape = Literal["native", "openai-compatible", "gemini-compatible"]


class OHMMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    name: str = Field(min_length=1)
    owner_organization_id: uuid.UUID
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

    # ── resolution helpers (pure) ──────────────────────────────────────────────
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
