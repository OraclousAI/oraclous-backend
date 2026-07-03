"""Team-draft service (services layer) — the compile → review → refine loop's persistence
home (#635, the re-scoped C-1(b)): the draft STORE. Every write re-runs the SHARED validator
(``validate_draft`` — the same capability-absence gate + assemble dry-run the
``core/manifest-validate@1`` registry tool wraps; one validator, ADR-047/#593) and embeds its
verdict ``{would_block, blocking, report}`` in the response, so the console's validation strip
reads it for free on every store round-trip.

Tenancy: org from the authenticated principal only (fail-closed 403); every repository call is
wrapped in ``org_scope`` so the ADR-030 RLS backstop bites. Errors are ``TeamRunError``-shaped
(curated message + status + leak-safe machine token) and map in the route like every sibling.
"""

from __future__ import annotations

import uuid
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.compiler import validate_draft
from oraclous_ohm.manifest import OHMManifest

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.compiler_onramp import draft_catalog
from oraclous_execution_engine_service.models.team_draft import EngineTeamDraft
from oraclous_execution_engine_service.repositories.team_draft_repository import (
    TeamDraftRepository,
)
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    load_team_manifest,
)


class DraftVerdict:
    """The shared validator's verdict — the EXACT wire shape the ``core/manifest-validate@1``
    registry tool returns (``{would_block, blocking[], report}``, report = the rendered dry-run;
    one validator, one shape — ADR-047/#593), so the console's validation strip reads the store's
    responses and the registry tool's interchangeably."""

    __slots__ = ("blocking", "report", "would_block")

    def __init__(self, *, would_block: bool, blocking: list[str], report: str) -> None:
        self.would_block = would_block
        self.blocking = blocking
        self.report = report

    @classmethod
    def from_validate(cls, verdict: dict[str, Any]) -> DraftVerdict:
        return cls(
            would_block=bool(verdict.get("would_block")),
            blocking=[str(b) for b in verdict.get("blocking", [])],
            report=str(verdict.get("report", "")),
        )


class TeamDraftService:
    def __init__(self, *, drafts: TeamDraftRepository) -> None:
        self._drafts = drafts

    # ── shared helpers ────────────────────────────────────────────────────────

    def _org(self, principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise TeamRunError("authenticated principal has no organisation scope", 403)
        return principal.organisation_id

    def _load_team(self, document: dict[str, Any]) -> OHMManifest:
        # the same inbound gate the run path applies (schema + version + entrypoint + acyclic DAG)
        return load_team_manifest(document)

    def _verdict(self, manifest_doc: dict[str, Any], manifest: OHMManifest) -> DraftVerdict:
        """Run the SHARED validator over the draft (ADR-047/#593 — the SAME ``validate_draft``
        the reviewer/registry tool runs: the capability-absence gate against the surveyed catalog
        + the assemble dry-run), so the store's verdict and the validator tool's agree."""
        verdict = validate_draft(
            manifest_doc,
            draft_catalog(),
            owner_organization_id=manifest.metadata.owner_organization_id,
            name=manifest.metadata.name,
        )
        return DraftVerdict.from_validate(verdict)

    async def _get_or_404(self, draft_id: uuid.UUID, org: uuid.UUID) -> EngineTeamDraft:
        with org_scope(org):
            row = await self._drafts.get(draft_id, org)
        if row is None:  # org-scoped: a cross-org draft is absent, not a 403
            raise TeamRunError("team draft not found", 404)
        return row

    # ── concern 1: the draft store ────────────────────────────────────────────

    async def create(
        self,
        principal: Principal,
        *,
        name: str,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
    ) -> tuple[EngineTeamDraft, DraftVerdict]:
        org = self._org(principal)
        team = self._load_team(manifest)
        verdict = self._verdict(manifest, team)
        with org_scope(org):
            row = await self._drafts.create(
                organisation_id=org,
                user_id=principal.principal_id,
                name=name,
                manifest=manifest,
                sub_harnesses=sub_harnesses,
            )
        return row, verdict

    async def get(
        self, draft_id: uuid.UUID, principal: Principal
    ) -> tuple[EngineTeamDraft, DraftVerdict]:
        org = self._org(principal)
        row = await self._get_or_404(draft_id, org)
        return row, self._verdict(row.manifest, self._load_team(row.manifest))

    async def list_for_org(
        self, principal: Principal, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        org = self._org(principal)
        bounded_limit = max(1, min(limit, 200))  # born bounded (WP-10) — mirror #633
        bounded_offset = max(0, offset)
        with org_scope(org):
            return await self._drafts.list_for_org(org, limit=bounded_limit, offset=bounded_offset)

    async def replace(
        self,
        draft_id: uuid.UUID,
        principal: Principal,
        *,
        name: str,
        manifest: dict[str, Any],
        sub_harnesses: dict[str, Any],
    ) -> tuple[EngineTeamDraft, DraftVerdict]:
        org = self._org(principal)
        team = self._load_team(manifest)
        verdict = self._verdict(manifest, team)
        with org_scope(org):
            row = await self._drafts.replace(
                draft_id, org, name=name, manifest=manifest, sub_harnesses=sub_harnesses
            )
        if row is None:
            raise TeamRunError("team draft not found", 404)
        return row, verdict

    async def delete(self, draft_id: uuid.UUID, principal: Principal) -> None:
        org = self._org(principal)
        with org_scope(org):
            deleted = await self._drafts.delete(draft_id, org)
        if not deleted:
            raise TeamRunError("team draft not found", 404)
