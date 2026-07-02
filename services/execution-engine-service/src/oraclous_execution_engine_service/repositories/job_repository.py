"""Engine job repository (repositories layer).

The only DB seam for engine job rows. Every read/write is org-scoped (ADR-006): writes carry the
resolved ``organisation_id`` and reads filter on it, so a tenant never reads another's jobs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.models.adopted_tool_run import AdoptedToolRun
from oraclous_execution_engine_service.models.enums import EngineJobState
from oraclous_execution_engine_service.models.job import EngineJob


class JobRepository:
    def __init__(
        self, db_url: str, *, worker_pool: bool = False, install_guard: bool = True
    ) -> None:
        # NullPool in the Celery worker: a task owns its connection and disposes it (ADR-012); never
        # share a pool across tasks. The request path uses the default pool.
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
        # ADR-030 §2: the ORG-BOUND engine carries the org-GUC guard so every transaction binds
        # app.current_organisation_id from the bound OrganisationContext (fail-closed to the empty
        # GUC → zero rows when none is bound). The request/driver path binds the org via the
        # principal / use_organisation_context before any query; the cross-org sweeps settle each
        # row under org_scope(row.org). `install_guard=False` is the MAINTENANCE reader on the owner
        # DSN (it bypasses RLS, so the guard would be inert anyway — skip it to mirror auth's
        # owner-engine credential store, which carries no guard).
        if install_guard:
            install_org_guc_guard(self._engine)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        input_text: str,
        manifest_ref: str | None = None,
        manifest_inline: dict[str, Any] | None = None,
        max_retries: int = 0,
        timeout_seconds: int | None = None,
        schedule_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> EngineJob:
        row = EngineJob(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            state=EngineJobState.QUEUED.value,
            manifest_ref=manifest_ref,
            manifest_inline=manifest_inline,
            input_text=input_text,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            schedule_id=schedule_id,
            idempotency_key=idempotency_key,
            progress=0,
            retry_count=0,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def create_scheduled(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        input_text: str,
        schedule_id: uuid.UUID,
        idempotency_key: str,
        manifest_inline: dict | None = None,
        manifest_ref: str | None = None,
    ) -> EngineJob | None:
        """Create a QUEUED job for a schedule fire — idempotent on ``(org, idempotency_key)``.
        Returns the row, or None if the window already fired (a duplicate tick); at-least-once."""
        try:
            return await self.create(
                organisation_id=organisation_id,
                user_id=user_id,
                input_text=input_text,
                manifest_inline=manifest_inline,
                manifest_ref=manifest_ref,
                schedule_id=schedule_id,
                idempotency_key=idempotency_key,
            )
        except IntegrityError:
            return None

    async def create_adopted_tool_run(
        self,
        *,
        organisation_id: uuid.UUID,
        schedule_id: uuid.UUID,
        idempotency_key: str,
    ) -> AdoptedToolRun | None:
        """Create the adopted-tool-run idempotency row for a schedule fire — idempotent on
        ``(org, idempotency_key)`` (#489). Returns the row, or None if the window already fired (a
        duplicate tick / a second fire-now). This row is the dedupe GATE: it is written
        transactionally BEFORE the registry dispatch is enqueued, so a None return means a second
        same-window dispatch is skipped (no double execution). ``execution_id`` is stamped later."""
        row = AdoptedToolRun(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            schedule_id=schedule_id,
            idempotency_key=idempotency_key,
        )
        try:
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row
        except IntegrityError:
            return None

    async def get_adopted_run(
        self, run_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> AdoptedToolRun | None:
        """#501: read an adopted-tool-run row (org-scoped). The worker's exactly-once REDELIVERY
        GUARD reads ``execution_id`` to short-circuit a re-dispatch — ``task_acks_late`` redelivers
        the task if a worker dies AFTER the registry dispatch succeeded but BEFORE the ack, and this
        adopted path (unlike the harness QUEUED→RUNNING CAS) has no other guard vs a 2nd run."""
        async with self._session() as session:
            result = await session.execute(
                select(AdoptedToolRun).where(
                    AdoptedToolRun.id == run_id,
                    AdoptedToolRun.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()

    async def claim_adopted_dispatch(
        self,
        run_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        """#501-#1: ATOMICALLY claim an adopted-tool dispatch — the exactly-once gate against
        CONCURRENT copies of one run (the original broker message + a lost-window reaper re-fire + a
        redelivery). A single conditional UPDATE stamps ``dispatched_at`` iff the run is unstamped
        AND unclaimed (or the claim aged past the lease — a crash mid-execute is re-claimable, so
        the reaper still recovers it). Postgres serialises concurrent UPDATEs on the row + re-checks
        the WHERE against the committed version, so EXACTLY ONE caller matches a row + proceeds to
        execute; the losers get rowcount 0 and short-circuit. Org-scoped (ADR-006)."""
        stale_before = now - timedelta(seconds=lease_seconds)
        async with self._session() as session:
            async with session.begin():
                # RETURNING the id (not rowcount): the CAS matched a row iff a row comes back — the
                # one caller whose UPDATE matched won the claim; concurrent losers get None.
                claimed = (
                    await session.execute(
                        sa_update(AdoptedToolRun)
                        .where(
                            AdoptedToolRun.id == run_id,
                            AdoptedToolRun.organisation_id == organisation_id,
                            AdoptedToolRun.execution_id.is_(None),
                            or_(
                                AdoptedToolRun.dispatched_at.is_(None),
                                AdoptedToolRun.dispatched_at < stale_before,
                            ),
                        )
                        .values(dispatched_at=now)
                        .returning(AdoptedToolRun.id)
                    )
                ).scalar_one_or_none()
            return claimed is not None

    async def set_adopted_execution_id(
        self, run_id: uuid.UUID, organisation_id: uuid.UUID, execution_id: uuid.UUID
    ) -> None:
        """Stamp the registry ExecutionOut.id onto the adopted-tool-run row AFTER the worker
        dispatched it (so a schedule's fires are auditable / readable). Org-scoped (ADR-006)."""
        async with self._session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AdoptedToolRun).where(
                            AdoptedToolRun.id == run_id,
                            AdoptedToolRun.organisation_id == organisation_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.execution_id = execution_id

    async def list_adopted_runs_for_schedule(
        self, schedule_id: uuid.UUID, organisation_id: uuid.UUID, *, limit: int = 100
    ) -> list[AdoptedToolRun]:
        """The adopted-tool-run rows a schedule has produced, newest-first (org-scoped). The
        readable proof a schedule fired + its stamped registry execution_id(s)."""
        async with self._session() as session:
            result = await session.execute(
                select(AdoptedToolRun)
                .where(
                    AdoptedToolRun.schedule_id == schedule_id,
                    AdoptedToolRun.organisation_id == organisation_id,
                )
                .order_by(AdoptedToolRun.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def create_event(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        input_text: str,
        idempotency_key: str,
        manifest_inline: dict | None = None,
        manifest_ref: str | None = None,
    ) -> EngineJob | None:
        """Create a QUEUED job for a webhook EVENT — idempotent on ``(org, idempotency_key)``, no
        ``schedule_id``. Returns the row, or None on a re-delivered event (the same key); the
        gateway delivery id is the dedupe key, so a webhook redelivery is a no-op."""
        try:
            return await self.create(
                organisation_id=organisation_id,
                user_id=user_id,
                input_text=input_text,
                manifest_inline=manifest_inline,
                manifest_ref=manifest_ref,
                idempotency_key=idempotency_key,
            )
        except IntegrityError:
            return None

    async def transition(
        self,
        job_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        new_state: str,
        allowed_from: frozenset[str],
        **fields: Any,
    ) -> tuple[EngineJob | None, bool]:
        """Atomic state transition under a row lock (so a concurrent cancel can't race the worker).

        Returns ``(row, applied)``: ``applied`` is False if the row is missing or its current state
        is not in ``allowed_from`` (the transition is a no-op — e.g. a terminal/cancelled job)."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    select(EngineJob)
                    .where(EngineJob.id == job_id, EngineJob.organisation_id == organisation_id)
                    .with_for_update()
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None, False
                if row.state not in allowed_from:
                    return row, False
                row.state = new_state
                for key, value in fields.items():
                    setattr(row, key, value)
            await session.refresh(row)
            return row, True

    async def get(self, job_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineJob | None:
        async with self._session() as session:
            result = await session.execute(
                select(EngineJob).where(
                    EngineJob.id == job_id, EngineJob.organisation_id == organisation_id
                )
            )
            return result.scalar_one_or_none()

    async def list_stale_running(
        self, older_than: datetime, *, limit: int = 100
    ) -> list[EngineJob]:
        """RUNNING jobs whose last update predates the lease — the reaper's system cross-org sweep.
        ADR-006 carve-out (precedent: auth-service's credential store / ADR-012): a no-principal
        maintenance op reads across orgs, but each reaped row is then settled + audited under its
        own organisation_id — no row crosses a tenant boundary."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineJob)
                .where(
                    EngineJob.state == EngineJobState.RUNNING.value,
                    EngineJob.updated_at < older_than,
                )
                .limit(limit)
            )
            return list(result.scalars().all())

    async def list_stale_adopted_runs(
        self, older_than: datetime, younger_than: datetime, *, limit: int = 100
    ) -> list[AdoptedToolRun]:
        """#501: adopted-tool-run rows whose registry dispatch never stamped an execution_id — the
        LOST-WINDOW recovery targets. ``created_at < older_than`` (past the dispatch lease, so an
        in-flight dispatch that stamps in seconds is never re-fired) AND ``created_at > younger``
        (bounded age, so a permanently-rejected instance can't re-fire forever). Also skips a row
        whose dispatch is CLAIMED within the lease (``dispatched_at`` fresh) — #501-#1: don't refire
        an in-flight-claimed dispatch (the claim would reject the re-fire anyway; this avoids the
        wasted message), while a STALE claim (a crash mid-execute) is re-fired. Cross-org on the
        MAINTENANCE engine (install_guard=False); each re-fire is settled under its own org."""
        async with self._session() as session:
            result = await session.execute(
                select(AdoptedToolRun)
                .where(
                    AdoptedToolRun.execution_id.is_(None),
                    AdoptedToolRun.created_at < older_than,
                    AdoptedToolRun.created_at > younger_than,
                    or_(
                        AdoptedToolRun.dispatched_at.is_(None),
                        AdoptedToolRun.dispatched_at < older_than,
                    ),
                )
                .limit(limit)
            )
            return list(result.scalars().all())

    async def list_for_org(
        self, organisation_id: uuid.UUID, *, state: str | None = None, limit: int = 50
    ) -> list[EngineJob]:
        stmt = select(EngineJob).where(EngineJob.organisation_id == organisation_id)
        if state is not None:
            stmt = stmt.where(EngineJob.state == state)
        stmt = stmt.order_by(EngineJob.created_at.desc()).limit(limit)
        async with self._session() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())
