"""Team-draft repository (repositories layer). Org-scoped (ADR-006); the org-GUC guard
(ADR-030) is installed on the engine, so every query is RLS-backstopped on the ``oraclous_app``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import cast as sa_cast
from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.models.team_draft import EngineTeamDraft

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult


class TeamDraftRepository:
    def __init__(
        self, db_url: str, *, worker_pool: bool = False, install_guard: bool = True
    ) -> None:
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
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
        name: str,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
    ) -> EngineTeamDraft:
        row = EngineTeamDraft(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            name=name,
            manifest=manifest,
            sub_harnesses=sub_harnesses,
            version=1,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def get(self, draft_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineTeamDraft | None:
        async with self._session() as session:
            result = await session.execute(
                select(EngineTeamDraft).where(
                    EngineTeamDraft.id == draft_id,
                    EngineTeamDraft.organisation_id == organisation_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_by_team_run(
        self, organisation_id: uuid.UUID, team_run_id: uuid.UUID
    ) -> EngineTeamDraft | None:
        """#638: the draft (if any) already peeled from this run — the from-run idempotency
        fast-path (a reload / second tab returns the existing draft, its CURRENT manifest even if
        since refined). Org-scoped (ADR-006) + RLS-backstopped (ADR-030)."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineTeamDraft).where(
                    EngineTeamDraft.organisation_id == organisation_id,
                    EngineTeamDraft.team_run_id == team_run_id,
                )
            )
            return result.scalar_one_or_none()

    async def create_from_run(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
        team_run_id: uuid.UUID,
    ) -> tuple[EngineTeamDraft, bool]:
        """#638: idempotent create for from-run — one draft per ``(organisation_id, team_run_id)``.
        Returns ``(row, created)``: ``(new, True)`` on a fresh insert; ``(existing, False)`` when a
        concurrent from-run for the SAME run won the partial-unique race, so two concurrent calls
        yield ONE row and the loser returns the winner's committed draft. Mirrors the team-run
        ``create_scheduled`` dedupe (try/IntegrityError). Org-scoped + RLS-backstopped."""
        row = EngineTeamDraft(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            name=name,
            manifest=manifest,
            sub_harnesses=sub_harnesses,
            version=1,
            team_run_id=team_run_id,
        )
        try:
            async with self._session() as session:
                async with session.begin():
                    session.add(row)
                await session.refresh(row)
                return row, True
        except IntegrityError:
            # the partial unique fired — a concurrent from-run inserted first; return the winner
            # (a fresh session/tx reads the committed row). None is a genuine anomaly → re-raise.
            existing = await self.get_by_team_run(organisation_id, team_run_id)
            if existing is None:
                raise
            return existing, False

    async def list_for_org(
        self,
        organisation_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """#635: the org's drafts — newest-first (``created_at DESC`` + an ``id`` tiebreaker so
        paging is stable when two drafts share a timestamp), paginated. Returns
        ``(page_rows, total)`` where ``total`` is the FULL matching count (ignoring
        ``limit``/``offset``) so a drafts table can paginate. Org-scoped (ADR-006) +
        RLS-backstopped (ADR-030). Projects ONLY the list-row columns — never ``manifest``/
        ``sub_harnesses`` — digging the member count out of ``manifest -> 'members'`` at query
        time, so a large manifest is never loaded (mirrors the #633 team-run list)."""
        condition = EngineTeamDraft.organisation_id == organisation_id
        async with self._session() as session:
            page = await session.execute(
                select(
                    EngineTeamDraft.id,
                    EngineTeamDraft.name,
                    EngineTeamDraft.version,
                    func.jsonb_array_length(
                        func.coalesce(
                            EngineTeamDraft.manifest["members"], sa_cast(literal("[]"), JSONB)
                        )
                    ).label("member_count"),
                    EngineTeamDraft.created_at,
                    EngineTeamDraft.updated_at,
                )
                .where(condition)
                .order_by(EngineTeamDraft.created_at.desc(), EngineTeamDraft.id.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = [dict(r._mapping) for r in page.all()]
            total = (
                await session.execute(
                    select(func.count()).select_from(EngineTeamDraft).where(condition)
                )
            ).scalar_one()
        return rows, int(total or 0)

    async def replace(
        self,
        draft_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        name: str,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
    ) -> EngineTeamDraft | None:
        """Full replace (PUT): swaps name/manifest/sub_harnesses and bumps ``version`` atomically.
        Returns the updated row, or ``None`` when the draft does not exist in this org."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    update(EngineTeamDraft)
                    .where(
                        EngineTeamDraft.id == draft_id,
                        EngineTeamDraft.organisation_id == organisation_id,
                    )
                    .values(
                        name=name,
                        manifest=manifest,
                        sub_harnesses=sub_harnesses,
                        version=EngineTeamDraft.version + 1,
                    )
                    .returning(EngineTeamDraft)
                )
                return result.scalar_one_or_none()

    async def update_documents(
        self,
        draft_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
    ) -> EngineTeamDraft | None:
        """A refine apply: swaps the manifest (+ the synthesized sub-harness map) and bumps
        ``version``, keeping the draft's name. ``None`` when the draft does not exist in this
        org."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    update(EngineTeamDraft)
                    .where(
                        EngineTeamDraft.id == draft_id,
                        EngineTeamDraft.organisation_id == organisation_id,
                    )
                    .values(
                        manifest=manifest,
                        sub_harnesses=sub_harnesses,
                        version=EngineTeamDraft.version + 1,
                    )
                    .returning(EngineTeamDraft)
                )
                return result.scalar_one_or_none()

    async def delete(self, draft_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        """True when a row was deleted; False when the draft does not exist in this org."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(EngineTeamDraft).where(
                        EngineTeamDraft.id == draft_id,
                        EngineTeamDraft.organisation_id == organisation_id,
                    )
                )
                return (cast("CursorResult[object]", result).rowcount or 0) > 0
