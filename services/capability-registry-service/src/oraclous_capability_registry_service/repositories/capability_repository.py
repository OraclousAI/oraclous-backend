"""Capability descriptor repository (repositories layer; reshape of legacy
``oraclous-core-service/app/repositories/capability_descriptor_repository.py``).

The ONLY place that touches the DB driver for capability descriptors. Every read and write is
scoped by ``organisation_id`` (ADR-006) — supplied by the caller from the authenticated principal,
never a request body (ORG001). Writes auto-compute ``content_hash`` (canonical SHA-256) and the
denormalised ``name`` unless the caller passes an explicit hash.

Global/platform tools: when a ``platform_org_id`` is configured, *reads* are widened to also return
descriptors owned by that platform org (the built-in catalogue), so every tenant org sees the global
tools alongside its own. ``id`` is a GLOBAL primary key, so a descriptor id resolves to at most one
row table-wide — reads de-duplicate defensively by id, and a tenant cannot register a tool whose
deterministic id collides with a platform built-in (the write raises ``CapabilityConflictError`` →
409, never a 500). *Writes* stay strict to the caller org (a tenant can never mutate a platform
built-in). When ``platform_org_id`` is None or equals the caller org, behaviour is exactly the old
single-org equality.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from oraclous_capability_registry_service.core.rls import build_rls_engine, org_scope
from oraclous_capability_registry_service.domain.hashing import compute_content_hash
from oraclous_capability_registry_service.domain.manifest import descriptor_name
from oraclous_capability_registry_service.models.base_model import Base
from oraclous_capability_registry_service.models.capability_descriptor import CapabilityDescriptor
from oraclous_capability_registry_service.models.enums import DescriptorKind

# Sentinel: "caller omitted content_hash" (auto-compute) vs "caller passed a value" (store as-is).
_AUTO: Any = object()


def status_for(descriptor: dict[str, Any], requested: str) -> str:
    """An MCP tool (``spec.type=="mcp"``) is ALWAYS created ``pending_approval`` — the supply-chain
    HITL gate, enforced HERE so it holds no matter the registration path (the admin ``import-mcp``
    flow OR the public member-level register/create routes) or a caller-passed status; only an
    admin's ``approve`` (set_status) makes it executable. Everything else uses ``requested``."""
    if (descriptor.get("spec") or {}).get("type") == "mcp":
        return "pending_approval"
    return requested


class CapabilityConflictError(Exception):
    """A descriptor id is already taken table-wide (e.g. a tenant tried to register a tool whose
    deterministic id collides with a platform built-in). Maps to HTTP 409."""


class CapabilityRepository:
    def __init__(self, db_url: str, *, platform_org_id: uuid.UUID | None = None) -> None:
        # ADR-030: build_rls_engine installs the org-GUC begin-guard so every transaction binds
        # app.current_organisation_id from the bound OrganisationContext (RLS backstop, T1-M1).
        self._engine = build_rls_engine(db_url, echo=False)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)
        self._platform_org_id = platform_org_id

    def _read_org_filter(self, organisation_id: uuid.UUID) -> Any:
        """Org-scope predicate for *reads*. Widened to the platform org (global tools) when one is
        configured and distinct from the caller; otherwise strict caller-org equality."""
        if self._platform_org_id is not None and self._platform_org_id != organisation_id:
            return CapabilityDescriptor.organisation_id.in_(
                (organisation_id, self._platform_org_id)
            )
        return CapabilityDescriptor.organisation_id == organisation_id

    def _dedupe_prefer_caller(
        self, rows: list[CapabilityDescriptor], organisation_id: uuid.UUID
    ) -> list[CapabilityDescriptor]:
        """Defensive de-dup of widened read results by id. With a GLOBAL primary key on ``id`` each
        id resolves to one row, so this is a no-op today; it is kept (preferring the caller-org row)
        so that if the schema ever moves to a composite (id, org) key the read stays single-valued
        per id. First-seen order is preserved."""
        if self._platform_org_id is None or self._platform_org_id == organisation_id:
            return rows
        by_id: dict[uuid.UUID, CapabilityDescriptor] = {}
        for row in rows:
            existing = by_id.get(row.id)
            if existing is None:
                by_id[row.id] = row
            elif (
                existing.organisation_id == self._platform_org_id
                and row.organisation_id == organisation_id
            ):
                # Replace the platform row with the caller-org row (caller shadows the built-in).
                by_id[row.id] = row
        return list(by_id.values())

    @property
    def engine(self) -> Any:
        """The underlying RLS-guarded ``AsyncEngine``. Exposed so the lifespan can run the ADR-030
        §3 runtime-role assertion against the same engine the request path uses (all repos build
        their engine on the same DSN/role, so asserting any one proves the runtime role for all)."""
        return self._engine

    async def create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(
        self,
        *,
        organisation_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: dict[str, Any],
        descriptor_id: uuid.UUID | None = None,
        content_hash: str | None = _AUTO,
        status: str = "active",
    ) -> CapabilityDescriptor:
        row = CapabilityDescriptor(
            id=descriptor_id or uuid.uuid4(),
            organisation_id=organisation_id,
            kind=kind,
            name=descriptor_name(descriptor),
            descriptor=descriptor,
            content_hash=(
                compute_content_hash(descriptor) if content_hash is _AUTO else content_hash
            ),
            status=status_for(descriptor, status),
        )
        # ADR-030: bind the caller's org so the engine begin-guard sets app.current_organisation_id;
        # without it the strict WITH CHECK on capability_descriptors denies the INSERT (42501) under
        # the oraclous_app role (writes stay strict to the caller org — a tenant can never write the
        # platform catalogue).
        with org_scope(organisation_id):
            async with self._session() as session:
                try:
                    async with session.begin():
                        session.add(row)
                except IntegrityError as exc:
                    raise CapabilityConflictError(
                        f"a capability with id {row.id} already exists"
                    ) from exc
                await session.refresh(row)
                return row

    async def set_status(
        self, *, descriptor_id: uuid.UUID, organisation_id: uuid.UUID, status: str
    ) -> bool:
        """Org-scoped status flip (R6 MCP-import approval). Returns True if a row was updated."""
        # ADR-030: bind the caller's org so RLS admits the row read + the strict WITH CHECK admits
        # the same-org UPDATE; the app-layer org equality below is preserved as defense-in-depth.
        with org_scope(organisation_id):
            async with self._session() as session, session.begin():
                row = await session.get(CapabilityDescriptor, descriptor_id)
                if row is None or row.organisation_id != organisation_id:
                    return False
                row.status = status
                return True

    async def set_status_if(
        self,
        *,
        descriptor_id: uuid.UUID,
        organisation_id: uuid.UUID,
        expected: str,
        status: str,
    ) -> bool:
        """Org-scoped *conditional* status flip: set ``status`` only when the row is currently
        ``expected`` (R6 MCP-import reject). Returns True only if a row was actually transitioned —
        an unknown id, a cross-org row, or a row already past ``expected`` all return False (so the
        caller masks them identically as a 404; an already-``active`` tool can't be silently
        reverted via the reject gate)."""
        # ADR-030: bind the caller's org so RLS admits the row read + the strict WITH CHECK admits
        # the same-org UPDATE; the app-layer org equality below is preserved as defense-in-depth.
        with org_scope(organisation_id):
            async with self._session() as session, session.begin():
                row = await session.get(CapabilityDescriptor, descriptor_id)
                if row is None or row.organisation_id != organisation_id or row.status != expected:
                    return False
                row.status = status
                return True

    async def upsert_by_id(
        self,
        *,
        organisation_id: uuid.UUID,
        descriptor_id: uuid.UUID,
        kind: DescriptorKind,
        descriptor: dict[str, Any],
    ) -> tuple[CapabilityDescriptor, str]:
        """Idempotently sync a descriptor by (id, org). Status is created|updated|unchanged.

        Used by plugin discovery: re-seeding the same descriptor is a no-op; a changed manifest
        (different content_hash) updates in place; a new descriptor is created.
        """
        new_hash = compute_content_hash(descriptor)
        # ADR-030: bind the org so RLS admits the read + the strict WITH CHECK admits the same-org
        # write. The startup catalogue seed already nests this under org_scope(PLATFORM_ORG); a
        # re-bind to the same org here is a harmless idempotent nest (the prior binding is restored
        # on exit), and the tenant path binds the caller org.
        with org_scope(organisation_id):
            async with self._session() as session:
                try:
                    async with session.begin():
                        result = await session.execute(
                            select(CapabilityDescriptor).where(
                                CapabilityDescriptor.id == descriptor_id,
                                CapabilityDescriptor.organisation_id == organisation_id,
                            )
                        )
                        row = result.scalars().first()
                        if row is None:
                            row = CapabilityDescriptor(
                                id=descriptor_id,
                                organisation_id=organisation_id,
                                kind=kind,
                                name=descriptor_name(descriptor),
                                descriptor=descriptor,
                                content_hash=new_hash,
                            )
                            session.add(row)
                            status = "created"
                        elif row.content_hash != new_hash:
                            row.descriptor = descriptor
                            row.name = descriptor_name(descriptor)
                            row.content_hash = new_hash
                            status = "updated"
                        else:
                            status = "unchanged"
                except IntegrityError as exc:
                    raise CapabilityConflictError(
                        f"a capability with id {descriptor_id} already exists"
                    ) from exc
                await session.refresh(row)
                return row, status

    async def get_by_id(
        self, descriptor_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> CapabilityDescriptor | None:
        # ADR-030: bind the caller's org so the GUC is the caller org; the widened-read RLS policy
        # then admits the caller's own rows AND the PLATFORM_ORG catalogue (its USING is
        # ``org=GUC OR org=PLATFORM``). The app-layer ``_read_org_filter`` is preserved on top.
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(CapabilityDescriptor).where(
                        CapabilityDescriptor.id == descriptor_id,
                        self._read_org_filter(organisation_id),
                    )
                )
                rows = self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)
                return rows[0] if rows else None

    async def list_by_org(self, organisation_id: uuid.UUID) -> list[CapabilityDescriptor]:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(CapabilityDescriptor)
                    .where(self._read_org_filter(organisation_id))
                    .order_by(CapabilityDescriptor.created_at)
                )
                return self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)

    async def list_by_kind(
        self, organisation_id: uuid.UUID, kind: DescriptorKind
    ) -> list[CapabilityDescriptor]:
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(CapabilityDescriptor)
                    .where(
                        self._read_org_filter(organisation_id),
                        CapabilityDescriptor.kind == kind,
                    )
                    .order_by(CapabilityDescriptor.created_at)
                )
                return self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)

    async def search_by_descriptor(
        self, organisation_id: uuid.UUID, filter_dict: dict[str, Any]
    ) -> list[CapabilityDescriptor]:
        """Org-scoped JSONB containment (``descriptor @> filter_dict``); widened to the platform
        org (global tools) when configured."""
        with org_scope(organisation_id):
            async with self._session() as session:
                result = await session.execute(
                    select(CapabilityDescriptor).where(
                        self._read_org_filter(organisation_id),
                        CapabilityDescriptor.descriptor.contains(filter_dict),
                    )
                )
                return self._dedupe_prefer_caller(list(result.scalars().all()), organisation_id)

    async def match_capabilities(
        self, organisation_id: uuid.UUID, capability_names: list[str]
    ) -> list[CapabilityDescriptor]:
        """Tool descriptors whose ``spec.capabilities`` includes any of ``capability_names``.

        Uses JSONB containment per name (``descriptor @> {"spec":{"capabilities":[{"name":n}]}}``),
        unioned by descriptor id with first-seen order preserved (deterministic). Reads are widened
        to the platform org (global tools); the caller-org row shadows a colliding built-in id.
        """
        seen: dict[uuid.UUID, CapabilityDescriptor] = {}
        for name in capability_names:
            rows = await self.search_by_descriptor(
                organisation_id, {"spec": {"capabilities": [{"name": name}]}}
            )
            for row in rows:
                existing = seen.get(row.id)
                if existing is None:
                    seen[row.id] = row
                elif (
                    self._platform_org_id is not None
                    and existing.organisation_id == self._platform_org_id
                    and row.organisation_id == organisation_id
                ):
                    # Caller-org row shadows the platform built-in seen under an earlier name.
                    seen[row.id] = row
        return list(seen.values())

    async def update_descriptor(
        self, descriptor_id: uuid.UUID, organisation_id: uuid.UUID, descriptor: dict[str, Any]
    ) -> CapabilityDescriptor | None:
        # ADR-030: bind the caller's org so RLS admits the read + the strict WITH CHECK admits the
        # same-org UPDATE (a tenant can only mutate its own descriptors, never the platform
        # catalogue); the app-layer org equality is preserved as defense-in-depth.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(CapabilityDescriptor).where(
                            CapabilityDescriptor.id == descriptor_id,
                            CapabilityDescriptor.organisation_id == organisation_id,
                        )
                    )
                    row = result.scalars().first()
                    if row is None:
                        return None
                    row.descriptor = descriptor
                    row.name = descriptor_name(descriptor)
                    row.content_hash = compute_content_hash(descriptor)
                # Reload the server-side onupdate `updated_at` before the session closes (else the
                # detached row triggers a refresh on attribute access — DetachedInstanceError).
                await session.refresh(row)
                return row

    async def delete(self, descriptor_id: uuid.UUID, organisation_id: uuid.UUID) -> bool:
        # ADR-030: bind the caller's org so RLS scopes the read/delete to this org (else the empty
        # GUC → no row found → False); the app-layer org equality is preserved as defense-in-depth.
        with org_scope(organisation_id):
            async with self._session() as session:
                async with session.begin():
                    result = await session.execute(
                        select(CapabilityDescriptor).where(
                            CapabilityDescriptor.id == descriptor_id,
                            CapabilityDescriptor.organisation_id == organisation_id,
                        )
                    )
                    row = result.scalars().first()
                    if row is None:
                        return False
                    await session.delete(row)
                return True
