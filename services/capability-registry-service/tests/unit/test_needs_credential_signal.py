"""Unit: O1 "no auth-prompt wall" — a missing required credential fails closed with a typed,
leak-safe ``needs_credential`` token (ADR-039). The caller learns EXACTLY which credential to
onboard (requirement_id + provider) and NEVER sees a secret value or a credential id (#483)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.plugins.builtin import WebResearchPlugin
from oraclous_capability_registry_service.schema.execution_schema import ExecuteRequest
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
)
from oraclous_capability_registry_service.services.tool_execution_service import (
    ExecutionNotReadyError,
    ToolExecutionService,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

_ORG = uuid.uuid4()
_CAP = uuid.uuid4()
_INST = uuid.uuid4()
_SECRET = "tvly-super-secret-byom-key-should-never-leak"  # noqa: S105 — test fixture, not a real key

# A keyed curated tool (web-research search is BYOM api_key, required) whose executor id is real,
# so execute_sync clears has_executor and reaches the credential-resolve loop.
_DESCRIPTOR = {
    "id": WebResearchPlugin.plugin_id(),
    "kind": "tool",
    "metadata": {"name": "core/web-research"},
    "spec": {
        "type": "web_research",
        "credential_requirements": [
            {"type": "api_key", "provider": "web_search", "required": True},
        ],
    },
}


class _FakeInstances:
    async def get_by_id(self, instance_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        # No credential_mappings → the broker cannot resolve api_key → a typed miss.
        return SimpleNamespace(
            id=_INST, capability_id=_CAP, organisation_id=_ORG, credential_mappings={}
        )


class _FakeCaps:
    async def get_by_id(self, capability_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        return SimpleNamespace(organisation_id=_ORG, status="active", descriptor=_DESCRIPTOR)


class _MissBroker:
    """The broker fails to resolve the unmapped api_key — exactly as the real one does on a miss.
    It NEVER returns the secret on this path; the test asserts the secret never reaches the body."""

    async def resolve(self, *, organisation_id, user_id, requirement, credential_id=None) -> Any:  # noqa: ANN001, ARG002
        raise CredentialResolutionError(
            "no credential is mapped for requirement api_key", error_code="credential_not_mapped"
        )


def _svc() -> ToolExecutionService:
    return ToolExecutionService(
        instances=_FakeInstances(), capabilities=_FakeCaps(), executions=None, broker=_MissBroker()
    )


async def test_missing_credential_yields_a_typed_needs_credential_token() -> None:
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _svc().execute_sync(
            instance_id=_INST,
            body=ExecuteRequest(input_data={"operation": "search", "query": "x"}),
            organisation_id=_ORG,
            user_id=uuid.uuid4(),
        )
    exc = ei.value
    # The specific reason discriminates the 409 (not overloaded onto pending_approval).
    assert exc.error_code == "credential_not_mapped"
    # The typed token tells the caller EXACTLY which credential to onboard — and nothing more.
    assert exc.detail["needs_credential"] == {"requirement_id": "api_key", "provider": "web_search"}


async def test_needs_credential_is_leak_safe_no_value_or_credential_id() -> None:
    with pytest.raises(ExecutionNotReadyError) as ei:
        await _svc().execute_sync(
            instance_id=_INST,
            body=ExecuteRequest(input_data={"operation": "search", "query": "x"}),
            organisation_id=_ORG,
            user_id=uuid.uuid4(),
        )
    # Serialise the whole 409 payload shape the factory handler emits ({error_code, **detail}).
    body = json.dumps({"error_code": ei.value.error_code, **ei.value.detail})
    token = ei.value.detail["needs_credential"]
    # The token carries ONLY requirement_id + provider — no value, credential id, or payload.
    assert set(token) == {"requirement_id", "provider"}
    assert _SECRET not in body
    assert "credential_id" not in token and "value" not in token and "payload" not in token
