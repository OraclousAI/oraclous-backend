"""Team-draft service (services layer) — the compile → review → refine loop's server side (#635,
the re-scoped C-1). Four Python-only ``packages/ohm`` steps move behind engine endpoints here so a
browser can drive the whole team loop:

- the draft STORE (create/get/list/replace/delete) — every write re-runs the SHARED validator
  (``assemble_and_report``, the one validator of ADR-047/#593) and embeds its verdict
  ``{would_block, blocking, report}`` in the response, so the console's validation strip reads it
  for free;
- DRAFT-FROM-RUN — peel the compiler reviewer's compiled JSON out of a SUCCEEDED team run,
  validate it through the same seam, synthesize per-member sub-harnesses, persist;
- REFINE — apply ONE typed op (``apply_refine``: preserve-the-rest guaranteed), re-validate,
  version-bump; a blocked op leaves the draft untouched;
- REFINE-NL — run the #595 op-drafter (a one-member team through the SAME ``create_team_run``
  path; the caller's BYOM models bound), peel its ONE typed op, then the same apply path.
  The op-drafter is a real LLM run, so the request polls it only up to a budget UNDER the
  gateway's upstream read timeout; a slower draft returns 202 + ``op_drafter_run_id`` and the
  caller re-calls with that id to collect (documented in the contract).

Tenancy: org from the authenticated principal only (fail-closed 403); every repository call is
wrapped in ``org_scope`` so the ADR-030 RLS backstop bites. Errors are ``TeamRunError``-shaped
(curated message + status + leak-safe machine token) and map in the route like every sibling.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.compiler import apply_refine, parse_op, validate_draft
from oraclous_ohm.compiler.prompts import OP_DRAFTER_PROMPT
from oraclous_ohm.import_ import ImportResult, assemble_and_report, render_report
from oraclous_ohm.import_.mapping import build_subharness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.models.team_draft import EngineTeamDraft
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.team_draft_repository import (
    TeamDraftRepository,
)
from oraclous_execution_engine_service.services.compiler_run_service import (
    surveyed_catalog,
    validate_model_bindings,
)
from oraclous_execution_engine_service.services.registry_client import RegistryClient
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
    load_team_manifest,
)

#: the states an op-drafter run can settle in (the poll's exit set)
_TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "FAILED", "REJECTED", "COST_BUDGET"})
#: the op-drafter member's role in its one-member team (#595)
_OP_DRAFTER_ROLE = "op-drafter"
#: the op-drafter team's manifest name — the collect token's fail-closed identity check
_OP_DRAFTER_TEAM_NAME = "refine-op-drafter"


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
    def from_result(cls, result: ImportResult) -> DraftVerdict:
        # would_block is a computed @property, so it is NOT in model_dump() — read it directly.
        return cls(
            would_block=result.report.would_block,
            blocking=list(result.report.blocking),
            report=render_report(result.report),
        )

    @classmethod
    def from_validate(cls, verdict: dict[str, Any]) -> DraftVerdict:
        return cls(
            would_block=bool(verdict.get("would_block")),
            blocking=[str(b) for b in verdict.get("blocking", [])],
            report=str(verdict.get("report", "")),
        )


class RefineOutcome:
    """A refine's result: the (possibly unchanged) draft row + verdict + what was decided."""

    __slots__ = ("applied", "op", "op_drafter_run_id", "row", "verdict")

    def __init__(
        self,
        *,
        row: EngineTeamDraft,
        verdict: DraftVerdict,
        applied: bool,
        op: dict[str, Any],
        op_drafter_run_id: uuid.UUID | None = None,
    ) -> None:
        self.row = row
        self.verdict = verdict
        self.applied = applied
        self.op = op
        self.op_drafter_run_id = op_drafter_run_id


class PendingOpDraft:
    """refine-nl's not-finished-yet outcome: the op-drafter run to collect on a follow-up call."""

    __slots__ = ("op_drafter_run_id",)

    def __init__(self, op_drafter_run_id: uuid.UUID) -> None:
        self.op_drafter_run_id = op_drafter_run_id


class TeamDraftService:
    def __init__(
        self,
        *,
        drafts: TeamDraftRepository,
        team_runs: TeamRunService,
        registry: RegistryClient | None = None,
        refine_nl_poll_seconds: float = 25.0,
        refine_nl_poll_interval_seconds: float = 2.0,
    ) -> None:
        self._drafts = drafts
        self._team_runs = team_runs
        self._registry = registry  # #638: live-registry union into the surveyed draft catalog
        self._poll_budget = refine_nl_poll_seconds
        self._poll_interval = refine_nl_poll_interval_seconds

    async def _catalog(self) -> list[str]:
        """#638: the surveyed catalog for the caller's org — seed inventory ∪ the live registry
        (degrades to seed-only on a registry outage). One seam behind validate/from-run/refine."""
        return await surveyed_catalog(self._registry)

    # ── shared helpers ────────────────────────────────────────────────────────

    def _org(self, principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise TeamRunError("authenticated principal has no organisation scope", 403)
        return principal.organisation_id

    def _load_team(self, document: dict[str, Any]) -> OHMManifest:
        # the same inbound gate the run path applies (schema + version + entrypoint + acyclic DAG)
        return load_team_manifest(document)

    async def _verdict(self, manifest_doc: dict[str, Any], manifest: OHMManifest) -> DraftVerdict:
        """Run the SHARED validator over the draft (ADR-047/#593 — the SAME ``validate_draft``
        the reviewer/registry tool runs: the capability-absence gate against the surveyed catalog
        + the assemble dry-run), so the store's verdict and the validator tool's agree. #638: the
        catalog now unions the org's LIVE registry, so a deployed connector validates admissible."""
        verdict = validate_draft(
            manifest_doc,
            await self._catalog(),
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
        verdict = await self._verdict(manifest, team)
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
        return row, await self._verdict(row.manifest, self._load_team(row.manifest))

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
        verdict = await self._verdict(manifest, team)
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
    ) -> tuple[EngineTeamDraft, DraftVerdict, bool]:
        """Peel the compiler reviewer's compiled JSON out of a SUCCEEDED run, validate it through
        the SAME seam the importer uses, synthesize per-member sub-harnesses, persist. An
        ineligible/unparseable run is a curated 422 — nothing persisted.

        #638 idempotency: ONE draft per ``(org, team_run_id)``. Returns ``(row, verdict, created)``
        — ``created`` is False (→ the route 200s) when the draft already existed (a reload / second
        tab on ``?compile=<runId>``, or a concurrent from-run that won the partial-unique race), so
        the client never duplicates the draft. The existing draft is returned AS-IS (its current
        manifest, even if since refined)."""
        org = self._org(principal)
        # fast-path: a draft already peeled from this run → return it (idempotent, current manifest)
        with org_scope(org):
            existing = await self._drafts.get_by_team_run(org, team_run_id)
        if existing is not None:
            verdict = await self._verdict(existing.manifest, self._load_team(existing.manifest))
            return existing, verdict, False
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
        # assemble dry-run) — a blocked compile is a curated 422, nothing persisted. #638: the
        # catalog now unions the org's LIVE registry (a deployed connector is admissible).
        gate = validate_draft(
            compiled, await self._catalog(), owner_organization_id=org, name=draft_name
        )
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
            row, created = await self._drafts.create_from_run(
                organisation_id=org,
                user_id=principal.principal_id,
                name=draft_name,
                manifest=result.manifest.model_dump(mode="json"),
                sub_harnesses=subs,
                team_run_id=team_run_id,
            )
        if not created:  # lost the partial-unique race — return the winner's draft + its verdict
            return row, await self._verdict(row.manifest, self._load_team(row.manifest)), False
        return row, DraftVerdict.from_validate(gate), True

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

    # ── concern 4: refine (typed op) + refine-nl (op-drafter) ────────────────

    async def refine(
        self,
        draft_id: uuid.UUID,
        principal: Principal,
        *,
        edit_op: dict[str, Any],
        dry_run: bool = False,
    ) -> RefineOutcome:
        """Apply ONE typed op through ``apply_refine`` (preserve-the-rest guaranteed), re-validate
        with the shared seam, bump the version. A blocked op (e.g. an unsurveyed tool) returns
        ``applied=False`` with the draft untouched. ``dry_run`` validates without persisting."""
        org = self._org(principal)
        row = await self._get_or_404(draft_id, org)
        return await self._apply_op(row, org, edit_op=edit_op, dry_run=dry_run)

    async def _apply_op(
        self,
        row: EngineTeamDraft,
        org: uuid.UUID,
        *,
        edit_op: dict[str, Any],
        dry_run: bool,
        op_drafter_run_id: uuid.UUID | None = None,
    ) -> RefineOutcome:
        manifest = self._load_team(row.manifest)
        try:
            op = parse_op(edit_op)
        except Exception as exc:  # noqa: BLE001 — a malformed op is a curated 422, never a 500
            raise TeamRunError(
                "edit_op is not one of the typed refine ops"
                " (add_member | set_fan_out | change_kind | add_depends_on)",
                422,
                error_type="invalid_edit_op",
            ) from exc
        result = apply_refine(
            manifest, op, catalog=await self._catalog(), owner_organization_id=org
        )
        verdict = DraftVerdict.from_result(result)
        if result.manifest is None or dry_run:
            return RefineOutcome(
                row=row,
                verdict=verdict,
                applied=False,
                op=edit_op,
                op_drafter_run_id=op_drafter_run_id,
            )
        subs = self._synthesize_subs(result.manifest, org, existing=dict(row.sub_harnesses))
        with org_scope(org):
            updated = await self._drafts.update_documents(
                row.id,
                org,
                manifest=result.manifest.model_dump(mode="json"),
                sub_harnesses=subs,
            )
        if updated is None:  # deleted from under us — surface the truth, nothing applied
            raise TeamRunError("team draft not found", 404)
        return RefineOutcome(
            row=updated,
            verdict=verdict,
            applied=True,
            op=edit_op,
            op_drafter_run_id=op_drafter_run_id,
        )

    async def refine_nl(
        self,
        draft_id: uuid.UUID,
        principal: Principal,
        *,
        instruction: str | None = None,
        models: list[dict[str, Any]] | None = None,
        op_drafter_run_id: uuid.UUID | None = None,
        dry_run: bool = False,
    ) -> RefineOutcome | PendingOpDraft:
        """NL refine (#595, server-side): run the op-drafter — a ONE-member team submitted through
        the SAME ``create_team_run`` path, the caller's BYOM models bound — peel its ONE typed op,
        then the same apply path as ``refine``. The op is a real LLM run: this call polls it only
        up to a budget below the gateway's upstream read timeout; if it hasn't settled, the caller
        gets the run id back (a 202 at the route) and re-calls with ``op_drafter_run_id`` to
        collect. ``dry_run=True`` drafts + validates WITHOUT applying (the preview → ``refine``
        applies the returned op)."""
        org = self._org(principal)
        row = await self._get_or_404(draft_id, org)
        if op_drafter_run_id is None:
            if not instruction or not instruction.strip():
                raise TeamRunError(
                    "refine-nl needs an instruction (or an op_drafter_run_id to collect)",
                    422,
                    error_type="missing_instruction",
                )
            bound_models = validate_model_bindings(models, who="refine-nl's op-drafter")
            run = await self._submit_op_drafter(
                principal,
                org,
                manifest=row.manifest,
                instruction=instruction.strip(),
                models=bound_models,
            )
            op_drafter_run_id = run.id
        settled = await self._await_op_drafter(op_drafter_run_id, principal)
        if settled is None:
            return PendingOpDraft(op_drafter_run_id)
        edit_op = self._peel_json(
            (settled.results or {}).get(_OP_DRAFTER_ROLE),
            who="the op-drafter",
            error_type="op_drafter_unparseable",
        )
        # the drafter may have taken seconds — re-fetch so the op applies to the CURRENT
        # document, never a pre-poll snapshot (a concurrent PUT/refine would otherwise be
        # silently clobbered by the stale write; the collect path re-fetches by construction).
        row = await self._get_or_404(draft_id, org)
        return await self._apply_op(
            row, org, edit_op=edit_op, dry_run=dry_run, op_drafter_run_id=op_drafter_run_id
        )

    async def _submit_op_drafter(
        self,
        principal: Principal,
        org: uuid.UUID,
        *,
        manifest: dict[str, Any],
        instruction: str,
        models: list[dict[str, Any]],
    ) -> EngineTeamRun:
        subgoal = (
            f"CURRENT TEAM MANIFEST:\n{json.dumps(manifest)}\n\n"
            f"SURVEYED CATALOG: {json.dumps(await self._catalog())}\n\n"
            f"EDIT REQUEST: {instruction}"
        )
        team = OHMManifest(
            ohm_version="1.1",
            metadata=OHMMetadata(
                id=uuid.uuid4(),
                name=_OP_DRAFTER_TEAM_NAME,
                owner_organization_id=org,
                kind="team",
            ),
            members=[
                OHMMember(
                    role=_OP_DRAFTER_ROLE,
                    kind="agent",
                    manifest_ref="org:refine/op-drafter@1",
                    subgoal=subgoal,
                )
            ],
            runtime=OHMRuntime(entrypoint=_OP_DRAFTER_ROLE),
        )
        doc = team.model_dump(mode="json")
        doc["models"] = models
        sub = build_subharness(
            _OP_DRAFTER_ROLE, owner_organization_id=org, body=OP_DRAFTER_PROMPT, tools=[]
        ).model_dump(mode="json")
        sub["models"] = models
        return await self._team_runs.create(
            principal,
            manifest=doc,
            sub_harnesses={_OP_DRAFTER_ROLE: sub},
            gate_decisions={},
        )

    async def _await_op_drafter(
        self, run_id: uuid.UUID, principal: Principal
    ) -> EngineTeamRun | None:
        """Poll the op-drafter run up to the budget. ``None`` = still driving (caller 202s);
        a non-SUCCEEDED terminal state is a curated 422 (the draft is untouched either way)."""
        deadline = time.monotonic() + self._poll_budget
        checked_shape = False
        while True:
            run = await self._team_runs.get(run_id, principal)  # 404 if not this org's run
            if not checked_shape:
                # fail-closed on run identity (first read, before any budget burns): the collect
                # token is only redeemable against an op-drafter run — an arbitrary same-org run
                # id (e.g. a compiler run) is a curated 422, not a 25s poll + a misleading peel.
                name = ((run.manifest or {}).get("metadata") or {}).get("name")
                if name != _OP_DRAFTER_TEAM_NAME:
                    raise TeamRunError(
                        "op_drafter_run_id does not name an op-drafter run",
                        422,
                        error_type="not_an_op_drafter_run",
                    )
                checked_shape = True
            if run.state in _TERMINAL_RUN_STATES:
                if run.state != "SUCCEEDED":
                    raise TeamRunError(
                        f"the op-drafter run did not succeed (state {run.state})",
                        422,
                        error_type="op_drafter_failed",
                    )
                return run
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll_interval)
