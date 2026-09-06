"""App repository (repositories layer, #932).

Org-scoped (ADR-006) with ONE deliberate asymmetry: reads admit the caller's organisation OR the
platform organisation that owns the Oraclous-provided apps; writes stay strictly the caller's. It
mirrors, in SQL, the widened policy migration 0026 puts on the table — the same pairing
capability-registry uses for the built-in tool catalogue.

Both layers matter and neither is redundant. The app-layer predicate is the primary control
(CLAUDE.md §3.3) and is what makes the widening explicit where a reader will see it; the database
policy is the backstop for any path that does not come through here. Writing only one of them would
leave the other silently wrong, which is why the raw-SQL tests prove the policy separately.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import cast as sa_cast
from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import ColumnElement

from oraclous_execution_engine_service.core.rls import install_org_guc_guard
from oraclous_execution_engine_service.domain.app_freeze import (
    fingerprint_documents,
    freeze_documents,
)
from oraclous_execution_engine_service.models.app import EngineApp

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult


class AppRepository:
    def __init__(
        self,
        db_url: str,
        *,
        platform_org_id: uuid.UUID,
        worker_pool: bool = False,
        install_guard: bool = True,
    ) -> None:
        kwargs = {"poolclass": NullPool} if worker_pool else {}
        self._engine = create_async_engine(db_url, echo=False, **kwargs)
        if install_guard:
            install_org_guc_guard(self._engine)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)
        self._platform_org_id = platform_org_id

    async def close(self) -> None:
        await self._engine.dispose()

    def _read_filter(self, organisation_id: uuid.UUID) -> ColumnElement[bool]:
        """The widened READ predicate: the caller's apps plus the Oraclous-provided ones.

        Collapses to strict equality when the caller IS the platform organisation, so the platform
        tenant does not accidentally read every other tenant's apps.
        """
        if organisation_id == self._platform_org_id:
            return EngineApp.organisation_id == organisation_id
        return EngineApp.organisation_id.in_((organisation_id, self._platform_org_id))

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        slug: str | None,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
        source_team_run_id: uuid.UUID | None = None,
        source_team_draft_id: uuid.UUID | None = None,
        source_draft_version: int | None = None,
        # Whose model key a run spends. Only "caller" is honoured today — the service refuses any
        # other value at run time — but it is settled when the app is made rather than patched on
        # later, because who pays is not something an app should change under its users.
        credentials_mode: str = "caller",
        # The authored form (#938) — the ordered fields a person saw and edited before saving.
        # ``None`` for a platform-seeded app (the seed never calls this with one), which is what
        # makes the read fall back to the #932 derived projection rather than an empty form.
        form: list[dict[str, Any]] | None = None,
    ) -> EngineApp:
        """Store an app, freezing its documents on the way in.

        Freezing here rather than in the service is deliberate: this is the only door into the
        table, so a credential cannot reach a row every organisation can read by way of some future
        caller that forgot to scrub first.
        """
        frozen = freeze_documents(manifest, sub_harnesses)
        row = EngineApp(
            id=uuid.uuid4(),
            organisation_id=organisation_id,
            user_id=user_id,
            name=name,
            description=description,
            slug=slug,
            manifest=frozen.manifest,
            sub_harnesses=frozen.sub_harnesses,
            manifest_fingerprint=frozen.fingerprint,
            pinned_version=1,
            source_team_run_id=source_team_run_id,
            source_team_draft_id=source_team_draft_id,
            source_draft_version=source_draft_version,
            credentials_mode=credentials_mode,
            form=form,
        )
        async with self._session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
            return row

    async def upsert_platform_app(
        self,
        *,
        slug: str,
        name: str,
        description: str | None,
        user_id: uuid.UUID,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
        app_id: uuid.UUID | None = None,
    ) -> EngineApp:
        """Seed or refresh one Oraclous-provided app. Runs under the platform org's own scope.

        Idempotent by FINGERPRINT, not by timestamp: the startup seed runs on every boot of every
        replica, and an unchanged app must not be rewritten — otherwise concurrent replicas race on
        a row nobody asked to change. A real content change updates in place and bumps
        ``pinned_version``, so the move is visible rather than silent.
        """
        frozen = freeze_documents(manifest, sub_harnesses)
        async with self._session() as session:
            async with session.begin():
                existing = (
                    await session.execute(
                        select(EngineApp).where(
                            EngineApp.organisation_id == self._platform_org_id,
                            EngineApp.slug == slug,
                        )
                    )
                ).scalar_one_or_none()

                if existing is None:
                    row = EngineApp(
                        id=app_id or uuid.uuid4(),
                        organisation_id=self._platform_org_id,
                        user_id=user_id,
                        name=name,
                        description=description,
                        slug=slug,
                        manifest=frozen.manifest,
                        sub_harnesses=frozen.sub_harnesses,
                        manifest_fingerprint=frozen.fingerprint,
                        pinned_version=1,
                    )
                    session.add(row)
                elif existing.manifest_fingerprint != frozen.fingerprint:
                    existing.name = name
                    existing.description = description
                    existing.manifest = frozen.manifest
                    existing.sub_harnesses = frozen.sub_harnesses
                    existing.manifest_fingerprint = frozen.fingerprint
                    existing.pinned_version = existing.pinned_version + 1
                    row = existing
                else:
                    return existing  # unchanged — no write at all, so no timestamp moves

            await session.refresh(row)
            return row

    async def get(self, app_id: uuid.UUID, organisation_id: uuid.UUID) -> EngineApp | None:
        """One app, under the WIDENED read — a platform app resolves for every organisation, which
        is what lets any tenant open the Validation Desk's page."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineApp).where(EngineApp.id == app_id, self._read_filter(organisation_id))
            )
            return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str, organisation_id: uuid.UUID) -> EngineApp | None:
        """The deep-link read: the console finds the Validation Desk by its stable handle rather
        than by a uuid pasted into an environment variable. Caller's app wins over a platform one
        of the same name, so an organisation can shadow a default with its own."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineApp)
                .where(EngineApp.slug == slug, self._read_filter(organisation_id))
                # the caller's own row sorts first: FALSE < TRUE, so the platform row comes last
                .order_by((EngineApp.organisation_id == self._platform_org_id).asc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def slug_exists(self, slug: str, organisation_id: uuid.UUID) -> bool:
        """Whether ``slug`` is already taken WITHIN the caller's own organisation (#938's slug
        ladder). Strict equality, not the widened read: ``uq_engine_apps_org_slug`` is unique per
        ``(organisation_id, slug)``, so a platform app's slug never collides with a tenant's own —
        checking the widened read here would refuse a free handle for no reason the index enforces.
        """
        async with self._session() as session:
            result = await session.execute(
                select(EngineApp.id).where(
                    EngineApp.organisation_id == organisation_id, EngineApp.slug == slug
                )
            )
            return result.first() is not None

    async def get_by_source_run(
        self, team_run_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> EngineApp | None:
        """The app already converted from this run, if any (#938) — so saving the same run twice
        returns the one app rather than a duplicate nobody can remove (no delete endpoint exists).
        Strict equality: only the caller's own conversion, never a platform app (which never carries
        a source run)."""
        async with self._session() as session:
            result = await session.execute(
                select(EngineApp).where(
                    EngineApp.organisation_id == organisation_id,
                    EngineApp.source_team_run_id == team_run_id,
                )
            )
            return result.scalar_one_or_none()

    async def list_for_org(
        self,
        organisation_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """The Apps tab: the caller's apps AND the Oraclous-provided ones, newest-first, paginated.

        Projects ONLY the list-row columns — never ``manifest``/``sub_harnesses`` — digging the
        member count out of the manifest at query time, so a tab never loads a five-member document
        per tile (the same discipline as the team-draft list).

        The page and the count share ONE predicate. Building them separately is how a tab ends up
        paginating against a total that never counted the platform apps.
        """
        condition = self._read_filter(organisation_id)
        async with self._session() as session:
            page = await session.execute(
                select(
                    EngineApp.id,
                    EngineApp.organisation_id,
                    EngineApp.name,
                    EngineApp.description,
                    EngineApp.slug,
                    EngineApp.pinned_version,
                    EngineApp.credentials_mode,
                    EngineApp.source_team_run_id,
                    func.jsonb_array_length(
                        func.coalesce(EngineApp.manifest["members"], sa_cast(literal("[]"), JSONB))
                    ).label("member_count"),
                    EngineApp.created_at,
                    EngineApp.updated_at,
                )
                .where(condition)
                .order_by(EngineApp.created_at.desc(), EngineApp.id.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = [dict(r._mapping) for r in page.all()]
            total = (
                await session.execute(select(func.count()).select_from(EngineApp).where(condition))
            ).scalar_one()
        return rows, int(total or 0)

    async def rename(
        self,
        app_id: uuid.UUID,
        organisation_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> EngineApp | None:
        """Rename or re-describe an app the caller OWNS. Strictly the caller's organisation, not the
        widened read: seeing a shared app must never imply being able to edit it for everyone.
        Returns ``None`` when no owned row matches — including for a platform app, which is how a
        tenant's attempt becomes a clean refusal rather than a raw policy error."""
        values: dict[str, Any] = {}
        if name is not None:
            values["name"] = name
        if description is not None:
            values["description"] = description
        if not values:
            return await self.get(app_id, organisation_id)
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    update(EngineApp)
                    .where(
                        EngineApp.id == app_id,
                        EngineApp.organisation_id == organisation_id,
                    )
                    .values(**values)
                    .returning(EngineApp)
                )
                return result.scalar_one_or_none()

    async def delete(self, app_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        """Delete an app the caller OWNS. Strict for the same reason ``rename`` is."""
        async with self._session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(EngineApp).where(
                        EngineApp.id == app_id,
                        EngineApp.organisation_id == organisation_id,
                    )
                )
            # AsyncSession.execute is typed Result; a DML statement returns a CursorResult (the only
            # variant carrying ``rowcount``). Cast to read the affected-row count (typed-service
            # convention, as schedule_repository.delete does).
            return (cast("CursorResult[object]", result).rowcount or 0) > 0

    def fingerprint(self, manifest: dict[str, Any], sub_harnesses: dict[str, Any]) -> str:
        """Exposed so a caller can ask "has this changed?" without writing."""
        return fingerprint_documents(manifest, sub_harnesses)
