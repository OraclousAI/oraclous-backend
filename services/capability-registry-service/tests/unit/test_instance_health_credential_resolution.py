"""Unit: an instance whose mapped credential no longer resolves is not Healthy and not READY (#692).

Found on the deployed stack (org `aebe595b-8071-446b-a166-1ff756dc7611`): instance
`40f596f9-6a0d-4aed-97d9-c8cb92e65dfb` mapped `api_key` to credential
`72801d24-df40-49ea-9de2-e45dbeed7f94`, a row that had been deleted. The instance stayed `READY`,
the console showed `credential configured`, and **Test connection reported Healthy** — a check that
actively tells the user the wrong thing. The truth only appeared 50k tokens into team run
`72ca031c-4226-41d7-8835-d70cf830b4ca`, as six identical broker 404s.

`ValidationService.validate_execution_readiness` backs both `/validate-execution` and `/health`, and
today it only checks that a mapping *exists* (presence, "live resolution is S4"). These tests make
it resolve the mapped credential through the broker seam, and make a failure move the instance out
of `READY` — the layering-clean half of #692 AC1. The alternative (the credential-broker clearing
`credential_mappings` rows inside the capability-registry on delete) is a cross-service cascade the
four-layer contract forbids, so it is deliberately not built.

The not-yet-built seam is the `broker=` argument to `ValidationService` and the resolve it performs;
`ValidationService` itself exists, so the module-level import is legitimate and only the tests fail.

RED until the #692 [impl] lands.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_capability_registry_service.models.enums import InstanceStatus
from oraclous_capability_registry_service.services.credential_client import (
    CredentialResolutionError,
    ResolvedCredential,
)
from oraclous_capability_registry_service.services.validation_service import ValidationService

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_CAP = uuid.uuid4()
_INST = uuid.uuid4()
_DEAD_CREDENTIAL_ID = "72801d24-df40-49ea-9de2-e45dbeed7f94"

_DESCRIPTOR = {
    "id": str(_CAP),
    "kind": "tool",
    "metadata": {"name": "GitHub Reader"},
    "spec": {
        "type": "API",
        "credential_requirements": [{"type": "api_key", "provider": "github", "required": True}],
    },
}
_NO_CREDENTIAL_DESCRIPTOR = {
    "id": str(_CAP),
    "kind": "tool",
    "metadata": {"name": "Recall Memory"},
    "spec": {"type": "INTERNAL", "credential_requirements": []},
}


class _FakeInstances:
    """Holds one mutable instance row and records every status write."""

    def __init__(self, *, status: InstanceStatus, mappings: dict[str, str] | None = None) -> None:
        self.row = SimpleNamespace(
            id=_INST,
            capability_id=_CAP,
            organisation_id=_ORG,
            user_id=_USER,
            configuration={},
            credential_mappings=dict(
                mappings if mappings is not None else {"api_key": _DEAD_CREDENTIAL_ID}
            ),
            required_credentials=["api_key"],
            status=status,
        )
        self.writes: list[tuple[dict[str, str], InstanceStatus]] = []

    async def get_by_id(self, instance_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        return self.row

    async def set_credentials_and_status(  # noqa: ANN201
        self,
        instance_id,  # noqa: ANN001, ARG002
        organisation_id,  # noqa: ANN001, ARG002
        credential_mappings: dict[str, str],
        status: InstanceStatus,
    ):
        self.writes.append((dict(credential_mappings), status))
        self.row.credential_mappings = dict(credential_mappings)
        self.row.status = status
        return self.row


class _FakeCaps:
    def __init__(self, descriptor: dict | None = None) -> None:
        self._descriptor = descriptor or _DESCRIPTOR

    async def get_by_id(self, capability_id, organisation_id):  # noqa: ANN001, ANN202, ARG002
        return SimpleNamespace(organisation_id=_ORG, status="active", descriptor=self._descriptor)


class _DeletedCredentialBroker:
    """The real broker's behaviour when `/internal/resolve-credential` 404s (the deleted row)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, *, organisation_id, user_id, requirement, credential_id=None) -> Any:  # noqa: ANN001
        self.calls.append(
            {
                "organisation_id": organisation_id,
                "user_id": user_id,
                "requirement": requirement,
                "credential_id": credential_id,
            }
        )
        raise CredentialResolutionError(
            "credential not found in the broker", error_code="credential_not_found"
        )


class _WorkingBroker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, *, organisation_id, user_id, requirement, credential_id=None) -> Any:  # noqa: ANN001, ARG002
        self.calls.append({"credential_id": credential_id})
        return ResolvedCredential(credential_type="api_key", payload={"api_key": "ghp_live"})


def _svc(
    instances: _FakeInstances, broker: Any, descriptor: dict | None = None
) -> ValidationService:
    return ValidationService(instances=instances, capabilities=_FakeCaps(descriptor), broker=broker)


async def _report(instances: _FakeInstances, broker: Any, descriptor: dict | None = None):  # noqa: ANN202
    return await _svc(instances, broker, descriptor).validate_execution_readiness(
        instance_id=_INST, organisation_id=_ORG
    )


# ── #692 AC2 — Test connection must never say Healthy for an unresolvable credential ──────────────


async def test_health_resolves_the_mapped_credential_through_the_broker() -> None:
    instances = _FakeInstances(status=InstanceStatus.READY)
    broker = _DeletedCredentialBroker()
    await _report(instances, broker)
    assert len(broker.calls) == 1
    assert broker.calls[0]["credential_id"] == _DEAD_CREDENTIAL_ID
    assert broker.calls[0]["requirement"]["type"] == "api_key"
    assert broker.calls[0]["organisation_id"] == _ORG  # org-scoped resolve, never cross-tenant


@pytest.mark.security
async def test_a_deleted_credential_reports_unhealthy_with_the_reason() -> None:
    report = await _report(_FakeInstances(status=InstanceStatus.READY), _DeletedCredentialBroker())
    assert report.is_ready is False
    assert report.checks["credentials"] == "failed"
    errors = [e for e in report.errors if e["type"] == "CREDENTIAL_UNRESOLVABLE"]
    assert errors, f"no CREDENTIAL_UNRESOLVABLE error in {report.errors}"
    error = errors[0]
    assert error["severity"] == "critical"
    assert error["credential_type"] == "api_key"
    # the reason must be readable — "which credential, and what is wrong with it"
    assert "api_key" in error["message"]
    # #483 envelope discipline: a credential id is never echoed back in the error token.
    assert _DEAD_CREDENTIAL_ID not in error["message"]


async def test_an_unresolvable_credential_offers_the_reconnect_action() -> None:
    report = await _report(_FakeInstances(status=InstanceStatus.READY), _DeletedCredentialBroker())
    actions = [a for a in report.action_items if a["credential_type"] == "api_key"]
    assert actions, f"no action item for api_key in {report.action_items}"
    assert actions[0]["action"] == "configure_credential"


async def test_a_resolvable_credential_still_reports_healthy() -> None:
    report = await _report(_FakeInstances(status=InstanceStatus.READY), _WorkingBroker())
    assert report.is_ready is True
    assert report.checks["credentials"] == "passed"
    assert report.status == InstanceStatus.READY


async def test_a_tool_needing_no_credential_never_calls_the_broker() -> None:
    broker = _WorkingBroker()
    instances = _FakeInstances(status=InstanceStatus.READY, mappings={})
    instances.row.required_credentials = []
    report = await _report(instances, broker, _NO_CREDENTIAL_DESCRIPTOR)
    assert report.is_ready is True
    assert broker.calls == []


# ── #692 AC1 — the instance must not stay READY ───────────────────────────────────────────────────


async def test_an_unresolvable_credential_moves_the_instance_out_of_ready() -> None:
    """The console renders CONFIGURATION_REQUIRED as needing attention. The mappings are left
    alone: clearing them would be the credential-broker reaching into the capability-registry's
    table, which the four-layer contract forbids."""
    instances = _FakeInstances(status=InstanceStatus.READY)
    report = await _report(instances, _DeletedCredentialBroker())
    assert report.status == InstanceStatus.CONFIGURATION_REQUIRED
    assert instances.writes, "the instance status was never persisted"
    mappings, status = instances.writes[-1]
    assert status == InstanceStatus.CONFIGURATION_REQUIRED
    assert mappings == {"api_key": _DEAD_CREDENTIAL_ID}  # the mapping is preserved, not cleared
    assert instances.row.status == InstanceStatus.CONFIGURATION_REQUIRED


async def test_reconnecting_the_credential_restores_ready() -> None:
    """The recovery half: once the user pastes a working key, a health check must put the instance
    back to READY rather than leaving it stuck in the attention state."""
    instances = _FakeInstances(status=InstanceStatus.CONFIGURATION_REQUIRED)
    report = await _report(instances, _WorkingBroker())
    assert report.is_ready is True
    assert instances.row.status == InstanceStatus.READY


async def test_a_healthy_instance_is_not_rewritten_on_every_check() -> None:
    """No status write when nothing changed — health is polled, and a write per poll is churn."""
    instances = _FakeInstances(status=InstanceStatus.READY)
    await _report(instances, _WorkingBroker())
    assert instances.writes == []
