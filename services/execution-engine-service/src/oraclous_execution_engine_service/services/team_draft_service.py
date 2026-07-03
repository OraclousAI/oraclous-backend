"""Team-draft service (services layer) — the compile → review → refine loop's persistence
home (#635, the re-scoped C-1): the draft STORE + DRAFT-FROM-RUN. Every write re-runs the SHARED
validator
(``validate_draft`` — the same capability-absence gate + assemble dry-run the
``core/manifest-validate@1`` registry tool wraps; one validator, ADR-047/#593) and embeds its
verdict ``{would_block, blocking, report}`` in the response, so the console's validation strip
reads it for free on every store round-trip.

Tenancy: org from the authenticated principal only (fail-closed 403); every repository call is
wrapped in ``org_scope`` so the ADR-030 RLS backstop bites. Errors are ``TeamRunError``-shaped
(curated message + status + leak-safe machine token) and map in the route like every sibling.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.compiler import validate_draft
from oraclous_ohm.import_ import assemble_and_report
from oraclous_ohm.import_.mapping import build_subharness
from oraclous_ohm.manifest import OHMManifest, OHMMember

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.compiler_onramp import draft_catalog
from oraclous_execution_engine_service.models.team_draft import EngineTeamDraft
from oraclous_execution_engine_service.repositories.team_draft_repository import (
    TeamDraftRepository,
)
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
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
    def __init__(self, *, drafts: TeamDraftRepository, team_runs: TeamRunService) -> None:
        self._drafts = drafts
        self._team_runs = team_runs

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

    @staticmethod
    def _synthesize_subs(
        manifest: OHMManifest,
        org: uuid.UUID,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A reasoning-only sub-harness per member that lacks one (the #594 e2e's client-side
        step, moved server-side): its sub-goal as the body, ``tools=[]`` — the validator already
        proved the declared tool ceilings resolve. Existing sub-harnesses are kept verbatim."""
        subs = dict(existing or {})
        for m in manifest.members:
            if m.role in subs or m.kind != "agent":
                continue
            subs[m.role] = build_subharness(
                m.role,
                owner_organization_id=org,
                body=(m.subgoal or f"You are the {m.role}. Complete your part of the objective."),
                tools=[],
            ).model_dump(mode="json")
        return subs

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

    # ── concern 3: draft-from-run ─────────────────────────────────────────────

    async def create_from_run(
        self,
        principal: Principal,
        *,
        team_run_id: uuid.UUID,
        name: str | None = None,
    ) -> tuple[EngineTeamDraft, DraftVerdict]:
        """Peel the compiler reviewer's compiled JSON out of a SUCCEEDED run, validate it through
        the SAME seam the importer uses, synthesize per-member sub-harnesses, persist. An
        ineligible/unparseable run is a curated 422 — nothing persisted."""
        org = self._org(principal)
        run = await self._team_runs.get(team_run_id, principal)  # org-scoped; 404 cross-org
        if run.state != "SUCCEEDED":
            raise TeamRunError(
                "only a SUCCEEDED run can seed a draft (the compiler reviewer's output is its"
                " final deliverable)",
                422,
                error_type="run_not_succeeded",
            )
        compiled = self._peel_json(
            (run.results or {}).get("reviewer"),
            who="the run's reviewer member",
            error_type="reviewer_output_unparseable",
        )
        raw_members = compiled.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            raise TeamRunError(
                "the reviewer's output carries no members[] — not a compiled team",
                422,
                error_type="reviewer_output_unparseable",
            )
        try:
            members = [OHMMember(**m) for m in raw_members]
        except Exception as exc:  # noqa: BLE001 — pydantic detail stays server-side (leak-safe)
            raise TeamRunError(
                "the reviewer's members[] do not validate as OHM team members",
                422,
                error_type="reviewer_output_unparseable",
            ) from exc
        draft_name = (name or "").strip() or f"compiled-{uuid.uuid4().hex[:8]}"
        # the SAME gate the reviewer runs (capability-absence vs the surveyed catalog + the
        # assemble dry-run) — a blocked compile is a curated 422, nothing persisted
        gate = validate_draft(compiled, draft_catalog(), owner_organization_id=org, name=draft_name)
        if gate.get("would_block"):
            raise TeamRunError(
                "the compiled team is not runnable: "
                + "; ".join(str(b) for b in gate.get("blocking", [])),
                422,
                error_type="compiled_team_blocked",
            )
        result = assemble_and_report(
            draft_name, members, owner_organization_id=org, shape="compiled"
        )
        if result.manifest is None:  # defensive — the gate above already proved assemblable
            raise TeamRunError(
                "the compiled team is not assemblable: " + "; ".join(result.report.blocking),
                422,
                error_type="compiled_team_blocked",
            )
        subs = self._synthesize_subs(result.manifest, org)
        with org_scope(org):
            row = await self._drafts.create(
                organisation_id=org,
                user_id=principal.principal_id,
                name=draft_name,
                manifest=result.manifest.model_dump(mode="json"),
                sub_harnesses=subs,
            )
        return row, DraftVerdict.from_validate(gate)

    @staticmethod
    def _peel_json(raw: Any, *, who: str, error_type: str) -> dict[str, Any]:
        """A member result is ``{"output": <text>, "status": ...}``; older shapes return the text
        directly. The JSON object is peeled out of the surrounding prose (the same regex the e2e
        proved) — anything else is a curated 422, never a 500."""
        if raw is None:
            raise TeamRunError(f"{who} produced no output", 422, error_type=error_type)
        text = raw.get("output") if isinstance(raw, dict) else raw
        if not isinstance(text, str) or not text.strip():
            raise TeamRunError(f"{who} produced no text output", 422, error_type=error_type)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise TeamRunError(f"{who} emitted no JSON object", 422, error_type=error_type)
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise TeamRunError(f"{who} emitted malformed JSON", 422, error_type=error_type) from exc
        if not isinstance(parsed, dict):
            raise TeamRunError(
                f"{who} emitted a non-object JSON payload", 422, error_type=error_type
            )
        return parsed
