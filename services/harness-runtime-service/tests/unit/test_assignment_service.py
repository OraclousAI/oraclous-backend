"""AssignmentService — claim/complete lifecycle (fakes, no I/O)."""

from __future__ import annotations

import uuid

import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.models.assignment import HarnessAssignment
from oraclous_harness_runtime_service.services.assignment_service import (
    AssignmentError,
    AssignmentService,
)

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_EXEC = uuid.uuid4()


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _assignment(status: str = "PENDING") -> HarnessAssignment:
    return HarnessAssignment(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        execution_id=_EXEC,
        harness_id=uuid.uuid4(),
        human_role="admin",
        status=status,
        input="review",
    )


class _FakeAssignments:
    def __init__(self, row: HarnessAssignment | None) -> None:
        self._row = row

    async def claim(self, _aid: uuid.UUID, _org: uuid.UUID) -> HarnessAssignment | None:
        return self._row

    async def complete(self, _aid: uuid.UUID, _org: uuid.UUID) -> HarnessAssignment | None:
        return self._row


class _FakeExecutions:
    def __init__(self) -> None:
        self.updates: list[tuple[uuid.UUID, str, str | None]] = []

    async def update_status(self, eid: uuid.UUID, _org: uuid.UUID, *, status: str, output=None):  # noqa: ANN001, ANN202
        self.updates.append((eid, status, output))
        return object()


class _FakeProv:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def emit(self, record) -> None:  # noqa: ANN001
        self.events.append((record.action, record.outcome))


def _svc(row: HarnessAssignment | None) -> tuple[AssignmentService, _FakeExecutions, _FakeProv]:
    execs, prov = _FakeExecutions(), _FakeProv()
    svc = AssignmentService(assignments=_FakeAssignments(row), executions=execs, provenance=prov)  # type: ignore[arg-type]
    return svc, execs, prov


async def test_claim_pending() -> None:
    svc, _, prov = _svc(_assignment("PENDING"))
    row = await svc.claim(uuid.uuid4(), _principal())
    assert row.status == "PENDING"  # the fake returns the row the repo CAS'd
    assert ("human.claim", "admin:CLAIMED") in prov.events


async def test_claim_not_claimable_raises() -> None:
    svc, _, _ = _svc(None)  # repo returns None (missing / wrong state)
    with pytest.raises(AssignmentError):
        await svc.claim(uuid.uuid4(), _principal())


async def test_complete_flips_the_execution() -> None:
    svc, execs, prov = _svc(_assignment("CLAIMED"))
    await svc.complete(uuid.uuid4(), _principal(), output="the human answer")
    assert execs.updates == [(_EXEC, "SUCCEEDED", "the human answer")]
    assert ("human.complete", "admin:COMPLETED") in prov.events


async def test_complete_not_found_raises() -> None:
    svc, execs, _ = _svc(None)
    with pytest.raises(AssignmentError):
        await svc.complete(uuid.uuid4(), _principal(), output="x")
    assert execs.updates == []  # the run is not touched if the assignment didn't transition


async def test_no_org_scope_raises() -> None:
    svc, _, _ = _svc(_assignment())
    with pytest.raises(AssignmentError):
        await svc.claim(uuid.uuid4(), _principal(org=None))
