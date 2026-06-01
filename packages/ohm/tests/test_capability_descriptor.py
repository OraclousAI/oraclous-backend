"""
[tests] OHM CapabilityDescriptor — kind discriminator + credential_requirements

Story: ORAA-68 / ORA-67
Architecture refs:
  - OHM v1.0 Spec:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Section 4:        https://oraclous.atlassian.net/wiki/spaces/OP/pages/425993
  - ADR-002:          https://oraclous.atlassian.net/wiki/spaces/OP/pages/557058
  - Test Strategy:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

Security tier: T2-M3 — credential_requirements declares scope (be-test-reviewer co-sign required)

Behaviours covered:
  B01  kind:tool descriptor validates with full spec
  B02  kind:skill descriptor validates with full spec
  B03  kind:agent descriptor validates with full spec
  B04  kind:harness descriptor validates with full spec
  B05  kind:human_role descriptor validates with full spec
  B06  invalid kind value is rejected at parse time
  B07  missing kind field is rejected
  B08  CapabilityDescriptor dispatches to correct subtype for each kind
  B09  existing ToolDefinition data is structurally representable as kind:tool
         NOTE: legacy-only fields (required on CredentialRequirement, spec.tags,
         spec.category) are acknowledged as not carried forward by the OHM schema.
         See ORAA-95 for rationale.
  B10  credential_requirements: oauth_token with scopes list validates (T2-M3)
  B11  credential_requirements: api_key without scopes validates
  B12  credential_requirements: connection_string without scopes validates
  B13  credential_requirements: username_password without scopes validates
  B14  credential_requirements: unknown/invalid credential type is rejected
  B15  credential_requirements: oauth_token with empty scopes list is rejected (scope must be declared)
  B16  credential_requirements: missing provider field is rejected
  B17  credential_requirements: multiple requirements in one tool descriptor validate
  B18  kind:tool with no credential_requirements (empty list) validates
  B19  kind:tool missing required input_schema is rejected
  B20  kind:tool missing required output_schema is rejected
  B21  kind:skill missing required instructions is rejected
  B22  kind:skill missing required loaded_when is rejected
  B23  kind:agent missing required role field is rejected
  B24  kind:harness missing required goal is rejected
  B25  kind:human_role missing required role_name is rejected

NOTE: All imports below will fail with ImportError until the implementer creates
      packages/ohm/. That failure is intentional — this file is written test-first.
"""

import pytest
from pydantic import TypeAdapter, ValidationError

# These imports will fail until packages/ohm/ is implemented.
# The ImportError is the expected initial test failure under TDD.
from ohm.schemas import (  # noqa: E402
    CapabilityDescriptor,
    ToolDescriptor,
    SkillDescriptor,
    AgentDescriptor,
    HarnessDescriptor,
    HumanRoleDescriptor,
)
from ohm.schemas.credential import CredentialRequirement, CredentialType  # noqa: E402

# ---------------------------------------------------------------------------
# Shared TypeAdapter — used for all discriminated-union validation
# ---------------------------------------------------------------------------
_ta: TypeAdapter = TypeAdapter(CapabilityDescriptor)


def _parse(data: dict) -> CapabilityDescriptor:
    return _ta.validate_python(data)


# ---------------------------------------------------------------------------
# Fixtures: minimal valid payloads for each kind
# ---------------------------------------------------------------------------

MINIMAL_TOOL: dict = {
    "kind": "tool",
    "id": "google-drive-reader",
    "version": {"hash": "sha256:abc123", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Google Drive Reader",
        "description": "Read files from Google Drive.",
    },
    "spec": {
        "implementation": {"type": "internal", "handler": "gdr.GoogleDriveReader"},
        "input_schema": {
            "type": "object",
            "required": ["file_id"],
            "properties": {"file_id": {"type": "string"}},
        },
        "output_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
        },
        "credential_requirements": [
            {"type": "oauth_token", "provider": "google", "scopes": ["drive.readonly"]}
        ],
    },
}

MINIMAL_SKILL: dict = {
    "kind": "skill",
    "id": "cold-outreach-drafter",
    "version": {"hash": "sha256:def456", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Cold Outreach Drafter",
        "description": "Draft cold outreach messages.",
    },
    "spec": {
        "loaded_when": "The actor needs to draft a cold outreach message.",
        "instructions": "# Cold Outreach\n\nDraft personalised messages.",
        "capability_requirements": [],
    },
}

MINIMAL_AGENT: dict = {
    "kind": "agent",
    "id": "outreach-drafter-agent",
    "version": {"hash": "sha256:ghi789", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Outreach Drafter",
        "description": "Drafts cold outreach across channels.",
    },
    "spec": {
        "role": "You are the Outreach Drafter. Draft messages for prospects.",
        "llm_config": {"provider_ref": "workspace-default"},
        "capabilities": [],
        "scope": {"workspaces": ["workspace-marketing"]},
    },
}

MINIMAL_HARNESS: dict = {
    "kind": "harness",
    "id": "outreach-pipeline",
    "version": {"hash": "sha256:jkl012", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Cold Outreach Pipeline",
        "description": "End-to-end outreach pipeline.",
    },
    "spec": {
        "goal": "Identify prospects, draft messages, get approval, and send.",
        "actors": [
            {
                "id": "drafter",
                "kind": "agent",
                "ref": {"id": "outreach-drafter-agent", "version_tag": "stable"},
            },
            {
                "id": "brand-reviewer",
                "kind": "human_role",
                "role": "brand_lead",
            },
        ],
        "orchestration": "1. Researcher finds prospects.\n2. Drafter drafts.\n3. Reviewer approves.",
    },
}

MINIMAL_HUMAN_ROLE: dict = {
    "kind": "human_role",
    "id": "brand-reviewer-role",
    "version": {"hash": "sha256:mno345", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Brand Reviewer",
        "description": "Human who reviews outreach drafts.",
    },
    "spec": {
        "role_name": "brand_lead",
        "fallback": {"role": "marketing_director"},
    },
}


# ---------------------------------------------------------------------------
# B01  kind:tool descriptor validates with full spec
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_descriptor_validates():
    """A tool descriptor with valid input/output schemas and credential_requirements parses."""
    result = _parse(MINIMAL_TOOL)
    assert isinstance(result, ToolDescriptor)
    assert result.kind == "tool"
    assert result.id == "google-drive-reader"


@pytest.mark.unit
def test_tool_descriptor_kind_field_is_literal():
    """ToolDescriptor.kind must be exactly 'tool'."""
    result = _parse(MINIMAL_TOOL)
    assert result.kind == "tool"


@pytest.mark.unit
def test_tool_descriptor_spec_contains_input_schema():
    """ToolDescriptor.spec carries the input_schema defined in the fixture."""
    result = _parse(MINIMAL_TOOL)
    assert result.spec.input_schema["type"] == "object"
    assert "file_id" in result.spec.input_schema["required"]


@pytest.mark.unit
def test_tool_descriptor_spec_contains_output_schema():
    """ToolDescriptor.spec carries the output_schema defined in the fixture."""
    result = _parse(MINIMAL_TOOL)
    assert result.spec.output_schema["type"] == "object"


# ---------------------------------------------------------------------------
# B02  kind:skill descriptor validates with full spec
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skill_descriptor_validates():
    """A skill descriptor with loaded_when and instructions parses successfully."""
    result = _parse(MINIMAL_SKILL)
    assert isinstance(result, SkillDescriptor)
    assert result.kind == "skill"


@pytest.mark.unit
def test_skill_descriptor_spec_contains_instructions():
    """SkillDescriptor.spec carries the instructions prose block."""
    result = _parse(MINIMAL_SKILL)
    assert "Cold Outreach" in result.spec.instructions


@pytest.mark.unit
def test_skill_descriptor_spec_contains_loaded_when():
    """SkillDescriptor.spec carries the loaded_when condition prose."""
    result = _parse(MINIMAL_SKILL)
    assert "cold outreach" in result.spec.loaded_when


# ---------------------------------------------------------------------------
# B03  kind:agent descriptor validates with full spec
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_descriptor_validates():
    """An agent descriptor with role, llm_config, and scope parses successfully."""
    result = _parse(MINIMAL_AGENT)
    assert isinstance(result, AgentDescriptor)
    assert result.kind == "agent"


@pytest.mark.unit
def test_agent_descriptor_spec_contains_role():
    """AgentDescriptor.spec carries the prose role definition."""
    result = _parse(MINIMAL_AGENT)
    assert "Outreach Drafter" in result.spec.role


@pytest.mark.unit
def test_agent_descriptor_spec_contains_scope():
    """AgentDescriptor.spec.scope lists the permitted workspaces."""
    result = _parse(MINIMAL_AGENT)
    assert "workspace-marketing" in result.spec.scope.workspaces


# ---------------------------------------------------------------------------
# B04  kind:harness descriptor validates with full spec
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_harness_descriptor_validates():
    """A harness descriptor with goal, actors, and orchestration parses successfully."""
    result = _parse(MINIMAL_HARNESS)
    assert isinstance(result, HarnessDescriptor)
    assert result.kind == "harness"


@pytest.mark.unit
def test_harness_descriptor_spec_contains_goal():
    """HarnessDescriptor.spec carries the prose goal."""
    result = _parse(MINIMAL_HARNESS)
    assert result.spec.goal


@pytest.mark.unit
def test_harness_descriptor_spec_has_actors_list():
    """HarnessDescriptor.spec.actors is a non-empty list."""
    result = _parse(MINIMAL_HARNESS)
    assert len(result.spec.actors) == 2


# ---------------------------------------------------------------------------
# B05  kind:human_role descriptor validates with full spec
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_human_role_descriptor_validates():
    """A human_role descriptor with role_name and fallback parses successfully."""
    result = _parse(MINIMAL_HUMAN_ROLE)
    assert isinstance(result, HumanRoleDescriptor)
    assert result.kind == "human_role"


@pytest.mark.unit
def test_human_role_descriptor_spec_role_name():
    """HumanRoleDescriptor.spec.role_name carries the role identifier."""
    result = _parse(MINIMAL_HUMAN_ROLE)
    assert result.spec.role_name == "brand_lead"


@pytest.mark.unit
def test_human_role_descriptor_spec_fallback():
    """HumanRoleDescriptor.spec.fallback carries the fallback role."""
    result = _parse(MINIMAL_HUMAN_ROLE)
    assert result.spec.fallback.role == "marketing_director"


# ---------------------------------------------------------------------------
# B06  invalid kind value is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_kind_value_is_rejected():
    """A descriptor whose kind is not one of the 5 valid values raises ValidationError."""
    data = {**MINIMAL_TOOL, "kind": "workflow"}
    with pytest.raises(ValidationError):
        _parse(data)


@pytest.mark.unit
def test_unknown_kind_string_is_rejected():
    """A nonsense kind string raises ValidationError — the discriminator has no matching branch."""
    data = {**MINIMAL_TOOL, "kind": "not_a_real_kind"}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B07  missing kind field is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_kind_field_is_rejected():
    """A descriptor payload without a kind field raises ValidationError."""
    data = {k: v for k, v in MINIMAL_TOOL.items() if k != "kind"}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B08  CapabilityDescriptor dispatches to the correct subtype for each kind
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,expected_type",
    [
        (MINIMAL_TOOL, ToolDescriptor),
        (MINIMAL_SKILL, SkillDescriptor),
        (MINIMAL_AGENT, AgentDescriptor),
        (MINIMAL_HARNESS, HarnessDescriptor),
        (MINIMAL_HUMAN_ROLE, HumanRoleDescriptor),
    ],
    ids=["tool", "skill", "agent", "harness", "human_role"],
)
def test_capability_descriptor_dispatches_to_correct_subtype(payload, expected_type):
    """CapabilityDescriptor discriminates on kind and returns the appropriate subtype."""
    result = _parse(payload)
    assert isinstance(result, expected_type)


# ---------------------------------------------------------------------------
# B09  existing ToolDefinition shape is structurally representable as kind:tool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_legacy_tool_definition_structurally_representable_as_tool_descriptor():
    """
    A legacy ToolDefinition payload (oraclous-core-service) parses as a valid
    kind:tool descriptor. Confirms structural compatibility so the migration from
    app/schemas/tool_definition.py does not break consumers.

    Acknowledged legacy-only fields NOT carried forward by the OHM schema
    (ORAA-95 — Option B: weaken claim rather than extend schema):
      - CredentialRequirement.required (True on api_key entry) — silently dropped
      - spec.tags (["ingestion", "etl"]) — silently dropped
      - spec.category ("INGESTION") — silently dropped
    These fields are intentionally out of scope for the OHM ToolDescriptor.
    """
    legacy_data = {
        "kind": "tool",
        "id": "legacy-data-ingestion-tool",
        "version": {"hash": "sha256:legacy001", "tags": ["1.0.0"]},
        "metadata": {
            "name": "Data Ingestion Tool",
            "description": "Ingest data from external sources.",
        },
        "spec": {
            "implementation": {
                "type": "internal",
                "handler": "ingestion.DataIngestionTool",
            },
            "input_schema": {
                "type": "object",
                "required": ["source_url"],
                "properties": {"source_url": {"type": "string"}},
            },
            "output_schema": {
                "type": "object",
                "properties": {"records_ingested": {"type": "integer"}},
            },
            "credential_requirements": [
                # required=True is a legacy-only field; OHM drops it silently.
                {"type": "api_key", "provider": "data-source", "required": True}
            ],
            # tags and category are legacy-only fields; OHM drops them silently.
            "tags": ["ingestion", "etl"],
            "category": "INGESTION",
        },
    }
    result = _parse(legacy_data)
    assert isinstance(result, ToolDescriptor)
    assert result.spec.input_schema["required"] == ["source_url"]
    assert result.spec.credential_requirements[0].type == CredentialType.API_KEY


# ---------------------------------------------------------------------------
# B10  credential_requirements: oauth_token + scopes validates  [T2-M3]
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_credential_requirement_oauth_token_with_scopes_validates():
    """An oauth_token credential requirement carrying a non-empty scopes list is valid (T2-M3)."""
    req = CredentialRequirement(
        type=CredentialType.OAUTH_TOKEN,
        provider="google",
        scopes=["drive.readonly", "gmail.send"],
    )
    assert req.type == CredentialType.OAUTH_TOKEN
    assert "drive.readonly" in req.scopes


# ---------------------------------------------------------------------------
# B11  credential_requirements: api_key without scopes validates
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_credential_requirement_api_key_without_scopes_validates():
    """An api_key credential requirement does not require a scopes list."""
    req = CredentialRequirement(type=CredentialType.API_KEY, provider="stripe")
    assert req.type == CredentialType.API_KEY
    assert req.scopes is None or req.scopes == []


# ---------------------------------------------------------------------------
# B12  credential_requirements: connection_string validates
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_credential_requirement_connection_string_validates():
    """A connection_string credential requirement validates without scopes."""
    req = CredentialRequirement(
        type=CredentialType.CONNECTION_STRING, provider="postgres"
    )
    assert req.type == CredentialType.CONNECTION_STRING


# ---------------------------------------------------------------------------
# B13  credential_requirements: username_password validates
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_credential_requirement_username_password_validates():
    """A username_password credential requirement validates without scopes."""
    req = CredentialRequirement(
        type=CredentialType.USERNAME_PASSWORD, provider="legacy-erp"
    )
    assert req.type == CredentialType.USERNAME_PASSWORD


# ---------------------------------------------------------------------------
# B14  credential_requirements: unknown type is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_credential_requirement_invalid_type_is_rejected():
    """An unrecognised credential type string raises ValidationError."""
    with pytest.raises(ValidationError):
        CredentialRequirement(type="magic_token", provider="wizard-service")


# ---------------------------------------------------------------------------
# B15  credential_requirements: oauth_token with empty scopes list is rejected  [T2-M3]
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_credential_requirement_oauth_token_empty_scopes_rejected():
    """
    An oauth_token requirement with an empty scopes list must be rejected.
    T2-M3 mandates that credential_requirements explicitly declare scope — an
    empty oauth scope list is an undeclared-scope credential and must not be
    permitted at schema validation time.
    """
    with pytest.raises(ValidationError):
        CredentialRequirement(
            type=CredentialType.OAUTH_TOKEN, provider="google", scopes=[]
        )


# ---------------------------------------------------------------------------
# B16  credential_requirements: missing provider is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_credential_requirement_missing_provider_rejected():
    """A credential requirement without a provider field raises ValidationError."""
    with pytest.raises(ValidationError):
        CredentialRequirement(
            type=CredentialType.OAUTH_TOKEN, scopes=["drive.readonly"]
        )


# ---------------------------------------------------------------------------
# B17  multiple credential requirements in one tool descriptor validate
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_tool_descriptor_multiple_credential_requirements():
    """A tool that needs both an API key and an OAuth token can declare both requirements."""
    data = {
        **MINIMAL_TOOL,
        "spec": {
            **MINIMAL_TOOL["spec"],
            "credential_requirements": [
                {"type": "api_key", "provider": "internal-registry"},
                {
                    "type": "oauth_token",
                    "provider": "google",
                    "scopes": ["drive.readonly"],
                },
            ],
        },
    }
    result = _parse(data)
    assert isinstance(result, ToolDescriptor)
    assert len(result.spec.credential_requirements) == 2


# ---------------------------------------------------------------------------
# B18  kind:tool with empty credential_requirements list validates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_descriptor_empty_credential_requirements_validates():
    """A tool that needs no credentials can declare an empty list — not every tool needs creds."""
    data = {
        **MINIMAL_TOOL,
        "spec": {**MINIMAL_TOOL["spec"], "credential_requirements": []},
    }
    result = _parse(data)
    assert isinstance(result, ToolDescriptor)
    assert result.spec.credential_requirements == []


# ---------------------------------------------------------------------------
# B19  kind:tool missing required input_schema is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_descriptor_missing_input_schema_rejected():
    """A tool descriptor without input_schema raises ValidationError — it is required."""
    spec_without_input = {
        k: v for k, v in MINIMAL_TOOL["spec"].items() if k != "input_schema"
    }
    data = {**MINIMAL_TOOL, "spec": spec_without_input}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B20  kind:tool missing required output_schema is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_descriptor_missing_output_schema_rejected():
    """A tool descriptor without output_schema raises ValidationError — it is required."""
    spec_without_output = {
        k: v for k, v in MINIMAL_TOOL["spec"].items() if k != "output_schema"
    }
    data = {**MINIMAL_TOOL, "spec": spec_without_output}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B21  kind:skill missing required instructions is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skill_descriptor_missing_instructions_rejected():
    """A skill descriptor without instructions raises ValidationError."""
    spec_without_instructions = {
        k: v for k, v in MINIMAL_SKILL["spec"].items() if k != "instructions"
    }
    data = {**MINIMAL_SKILL, "spec": spec_without_instructions}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B22  kind:skill missing required loaded_when is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skill_descriptor_missing_loaded_when_rejected():
    """A skill descriptor without loaded_when raises ValidationError."""
    spec_without_lw = {
        k: v for k, v in MINIMAL_SKILL["spec"].items() if k != "loaded_when"
    }
    data = {**MINIMAL_SKILL, "spec": spec_without_lw}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B23  kind:agent missing required role field is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_descriptor_missing_role_rejected():
    """An agent descriptor without a role prose field raises ValidationError."""
    spec_without_role = {k: v for k, v in MINIMAL_AGENT["spec"].items() if k != "role"}
    data = {**MINIMAL_AGENT, "spec": spec_without_role}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B24  kind:harness missing required goal is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_harness_descriptor_missing_goal_rejected():
    """A harness descriptor without a goal prose field raises ValidationError."""
    spec_without_goal = {
        k: v for k, v in MINIMAL_HARNESS["spec"].items() if k != "goal"
    }
    data = {**MINIMAL_HARNESS, "spec": spec_without_goal}
    with pytest.raises(ValidationError):
        _parse(data)


# ---------------------------------------------------------------------------
# B25  kind:human_role missing required role_name is rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_human_role_descriptor_missing_role_name_rejected():
    """A human_role descriptor without role_name raises ValidationError."""
    spec_without_role_name = {
        k: v for k, v in MINIMAL_HUMAN_ROLE["spec"].items() if k != "role_name"
    }
    data = {**MINIMAL_HUMAN_ROLE, "spec": spec_without_role_name}
    with pytest.raises(ValidationError):
        _parse(data)
