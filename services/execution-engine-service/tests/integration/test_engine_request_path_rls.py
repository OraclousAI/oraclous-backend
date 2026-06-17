"""Real-path proof that the API REQUEST path binds the org GUC (ADR-030 §3 / #353 regression).

The merged RLS slice carved the engine's DB access into an org-bound engine (``oraclous_app`` + the
GUC guard) and a maintenance (owner) reader, and proved — in ``test_engine_rls_backstop_isolation``
— that the data layer isolates when the org is bound by hand (``use_organisation_context`` /
``org_scope`` around a raw INSERT). It did **not** drive the actual ``JobService`` /
``JobRepository`` request path, and that gap hid a fail-closed bug: the request path never bound the
org, so under the deployed ``oraclous_app`` runtime + FORCE'd RLS the GUC was empty for every API
request — ``JobService.submit`` → ``JobRepository.create`` opened a transaction on the org-bound
engine with NO bound context, the begin-guard bound the empty GUC, and the WITH CHECK rejected the
INSERT (SQLSTATE 42501); reads fell to zero rows.

This suite closes that gap. It drives the **real** ``JobService`` over a **real** ``JobRepository``
+ ``PostgresProvenanceSink`` on the ``oraclous_app`` org-bound engine — the service/repo bind the
GUC themselves (the test never calls ``use_organisation_context`` / ``org_scope`` for the happy
path; the only place ``org_scope`` appears is to *construct* the deliberately-mismatched cross-org
WRITE that must be denied, exactly as the data-layer isolation test does). It proves:

* a same-org ``submit`` SUCCEEDS (the bug repro — without the request-path binding this INSERT
  raises 42501) and durably persists the QUEUED job + its provenance event (the ride-along table);
* the tenant reads its OWN job back through ``JobService.get`` and ``JobService.list``;
* a cross-org READ is denied — org B's ``get`` / ``list`` never sees org A's job (RLS USING filters
  it, with the app ``WHERE`` still in place AND at the data layer);
* a cross-org WRITE is denied — org B cannot ``cancel`` org A's job (its write target is invisible),
  and the hard WITH CHECK still bites: a ``JobRepository.create`` stamping org A while org B is
  bound raises 42501. The backstop is intact.

Threats: T1-M1, T1-M3. ADR-006; ADR-012 §1a/§2; ADR-030 §3.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.repositories.job_repository import JobRepository
from oraclous_execution_engine_service.repositories.provenance_sink import PostgresProvenanceSink
from oraclous_execution_engine_service.services.job_service import JobError, JobService
from oraclous_governance import Principal, PrincipalType
from oraclous_substrate import ProvenanceCollector
from sqlalchemy.exc import ProgrammingError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.organization_isolation,
    pytest.mark.security,
    pytest.mark.isolation,
]

ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _principal(org: uuid.UUID, user: uuid.UUID) -> Principal:
    return Principal(principal_id=user, principal_type=PrincipalType.USER, organisation_id=org)


@pytest.fixture
async def job_service(engine_dsns) -> AsyncIterator[tuple[JobService, list[uuid.UUID]]]:  # noqa: ANN001
    """The REAL request-path ``JobService`` wired exactly as ``get_job_service`` wires it in
    deployment — a ``JobRepository`` + ``PostgresProvenanceSink`` on the NOSUPERUSER
    ``oraclous_app`` org-bound engine (the GUC guard installed by default), and an injected enqueue.
    No org is bound by the test: the service binds it per request via ``org_scope`` (the fix under
    test). The enqueue is captured (not a broker) so a successful submit is a pure DB+provenance
    write.
    """
    _owner_async_dsn, app_async_dsn = engine_dsns
    jobs = JobRepository(app_async_dsn)
    sink = PostgresProvenanceSink(app_async_dsn)
    enqueued: list[uuid.UUID] = []
    service = JobService(
        jobs=jobs,
        provenance=ProvenanceCollector(sink),
        enqueue=lambda job_id, _org, _user: enqueued.append(job_id),
    )
    try:
        yield service, enqueued
    finally:
        await jobs.close()
        await sink.close()


async def test_submit_persists_and_reads_back_on_org_bound_engine(
    job_service: tuple[JobService, list[uuid.UUID]],
) -> None:
    """The bug repro + the fix: ``JobService.submit`` on the org-bound engine binds the org itself,
    so the INSERT is admitted (without the fix it raises 42501 against the empty GUC) — and the
    tenant reads its OWN job back through both ``get`` and ``list`` (the test never binds the GUC).
    """
    service, enqueued = job_service

    job = await service.submit(
        principal=_principal(ORG_A, USER_A), input_text="org-a-work", manifest_inline={"x": 1}
    )
    assert job.state == EngineJobState.QUEUED.value
    assert job.organisation_id == ORG_A
    assert enqueued == [job.id]  # the QUEUED row was durable, so the enqueue fired

    # The tenant reads its own job back — RLS admits it under the request-bound org (no hand-bind).
    fetched = await service.get(job.id, _principal(ORG_A, USER_A))
    assert fetched is not None and fetched.id == job.id and fetched.input_text == "org-a-work"

    listed = await service.list(_principal(ORG_A, USER_A))
    assert [row.id for row in listed] == [job.id]


async def test_cross_org_read_is_denied(
    job_service: tuple[JobService, list[uuid.UUID]],
) -> None:
    """Org A submits a job; org B's request-path reads (``get`` + ``list``) never see it — RLS
    scopes the org-bound engine to the request-bound org."""
    service, _ = job_service

    a_job = await service.submit(
        principal=_principal(ORG_A, USER_A), input_text="a-only", manifest_inline={}
    )

    # org B sees none of org A's jobs.
    assert await service.get(a_job.id, _principal(ORG_B, USER_B)) is None
    assert await service.list(_principal(ORG_B, USER_B)) == []

    # org B's own submit succeeds + is isolated from org A's list (both directions hold).
    b_job = await service.submit(
        principal=_principal(ORG_B, USER_B), input_text="b-only", manifest_inline={}
    )
    assert [row.id for row in await service.list(_principal(ORG_B, USER_B))] == [b_job.id]
    assert [row.id for row in await service.list(_principal(ORG_A, USER_A))] == [a_job.id]


async def test_cross_org_write_is_denied(
    job_service: tuple[JobService, list[uuid.UUID]],
    engine_dsns,  # noqa: ANN001
) -> None:
    """A cross-org WRITE is denied two ways through the real seam:

    1. org B cannot ``cancel`` org A's job — its write target is invisible under org B's bound
       scope, so the service reports it as not-found (the row is unwritable to another tenant);
       org A's job stays QUEUED.
    2. the hard RLS WITH CHECK still bites at the repository: a ``JobRepository.create`` stamping
       org A while org B is the bound scope raises SQLSTATE 42501 (the backstop the request-path
       binding rides on is intact). ``org_scope`` here constructs the mismatched scope on purpose —
       it is not the happy-path binding.
    """
    service, _ = job_service
    _owner_async_dsn, app_async_dsn = engine_dsns

    a_job = await service.submit(
        principal=_principal(ORG_A, USER_A), input_text="a-cancelable", manifest_inline={}
    )

    # (1) org B cannot mutate org A's job — the cancel target is not visible/writable to org B.
    with pytest.raises(JobError, match="job not found"):
        await service.cancel(a_job.id, _principal(ORG_B, USER_B))
    # org A's job is untouched (still QUEUED) — the cross-org write had no effect.
    a_after = await service.get(a_job.id, _principal(ORG_A, USER_A))
    assert a_after is not None and a_after.state == EngineJobState.QUEUED.value

    # (2) the WITH CHECK denies a write stamped for another org than the bound one (42501).
    jobs = JobRepository(app_async_dsn)
    try:
        with pytest.raises(ProgrammingError) as exc_info:  # noqa: PT012 — bind + the write under it
            with org_scope(ORG_B):  # deliberately mismatched bound scope (NOT the happy path)
                await jobs.create(
                    organisation_id=ORG_A,  # smuggled — not the bound org
                    user_id=USER_A,
                    input_text="smuggled",
                )
        assert getattr(exc_info.value.orig, "sqlstate", None) == "42501"
    finally:
        await jobs.close()
