from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from .credential import CredentialRequirement


class _VersionInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hash: str
    tags: list[str] = []


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    description: str


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class _ToolImplementation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    handler: str


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    implementation: _ToolImplementation
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    credential_requirements: list[CredentialRequirement] = []


class ToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["tool"]
    id: str
    version: _VersionInfo
    metadata: _Metadata
    spec: ToolSpec


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


class SkillSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    loaded_when: str
    instructions: str
    capability_requirements: list[Any] = []


class SkillDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["skill"]
    id: str
    version: _VersionInfo
    metadata: _Metadata
    spec: SkillSpec


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class _AgentScope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    workspaces: list[str] = []


class _AgentLlmConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider_ref: str


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str
    llm_config: Optional[_AgentLlmConfig] = None
    capabilities: list[Any] = []
    scope: Optional[_AgentScope] = None


class AgentDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["agent"]
    id: str
    version: _VersionInfo
    metadata: _Metadata
    spec: AgentSpec


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _HarnessActor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    kind: str
    ref: Optional[dict[str, Any]] = None
    role: Optional[str] = None


class HarnessSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    goal: str
    actors: list[_HarnessActor] = []
    orchestration: Optional[str] = None


class HarnessDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["harness"]
    id: str
    version: _VersionInfo
    metadata: _Metadata
    spec: HarnessSpec


# ---------------------------------------------------------------------------
# HumanRole
# ---------------------------------------------------------------------------


class _HumanRoleFallback(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str


class HumanRoleSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role_name: str
    fallback: Optional[_HumanRoleFallback] = None


class HumanRoleDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["human_role"]
    id: str
    version: _VersionInfo
    metadata: _Metadata
    spec: HumanRoleSpec


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

CapabilityDescriptor = Annotated[
    Union[
        ToolDescriptor,
        SkillDescriptor,
        AgentDescriptor,
        HarnessDescriptor,
        HumanRoleDescriptor,
    ],
    Field(discriminator="kind"),
]
