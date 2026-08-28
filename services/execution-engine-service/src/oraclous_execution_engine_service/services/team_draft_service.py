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
import time
import uuid
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.compiler import apply_refine, parse_op, validate_draft
from oraclous_ohm.compiler.prompts import OP_DRAFTER_PROMPT
from oraclous_ohm.import_ import ImportResult, assemble_and_report, render_report
from oraclous_ohm.import_.mapping import build_subharness, remap_capability_refs
from oraclous_ohm.manifest import (
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
)
from pydantic import ValidationError

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.model_answer import first_json_object
from oraclous_execution_engine_service.models.team_draft import EngineTeamDraft
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.team_draft_repository import (
    TeamDraftRepository,
)
from oraclous_execution_engine_service.services.compiler_run_service import (
    surveyed_catalog,
    surveyed_catalog_described,
    validate_model_bindings,
)
from oraclous_execution_engine_service.services.registry_client import (
    RegistryClient,
    RegistryClientError,
)
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
#: a fenced ``` / ```json block — the shape the compiler prompts ask a member to answer in
# #866: the peel moved to the domain layer so the intake read-back reuses it verbatim rather
# than growing a second, subtly different parser. Kept under the local name its callers use.
_first_json_object = first_json_object


def _maybe_orchestration(raw: Any) -> OHMOrchestration | None:
    """The drafted ``orchestration``, or None when it is absent or junk. ``assemble_and_report``
    wants the typed object here (unlike the untyped governance/budget/task_input threads), and a
    model-authored block cannot be trusted to validate — a malformed one is dropped, never raised.
    The reviewer's gate already blocks it upstream, so this is the defensive floor."""
    if not isinstance(raw, dict):
        return None
    try:
        return OHMOrchestration.model_validate(raw)
    except ValidationError:
        return None


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
        (degrades to seed-only on a registry outage). One seam behind validate/from-run/refine.

        Bare slugs, deliberately: every caller of this is a VALIDATOR (the capability-absence gate
        diffs a draft's tools against it). The described view below is for prompts only."""
        return await surveyed_catalog(self._registry)

    async def _described_catalog(self) -> list[dict[str, str]]:
        """#713: the same catalog with each tool's description, for the text a MODEL reads."""
        return await surveyed_catalog_described(self._registry)

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
        """A sub-harness per member that lacks one (the #594 e2e's client-side step, moved
        server-side): its sub-goal as the body, and its DECLARED tools as the grant.

        #659: the member's ``tools[]`` is only the deny-by-default CEILING — it grants nothing. The
        harness builds the model's toolset from the sub-harness ``capabilities[]`` alone, so
        synthesizing with ``tools=[]`` dispatched every compiled member with an empty toolset (run
        ``0fc1f7f1``: nine members, zero tool calls) while the ceiling check passed trivially,
        because an empty grant is inside any ceiling. For a SYNTHESIZED sub-harness the only
        sensible grant is exactly what the member declared, so the grant is the ceiling — and stays
        within it by construction. A hand-authored or imported sub-harness may still narrow below
        the ceiling; existing sub-harnesses are kept verbatim. A tool-less member still yields a
        loadable reasoning-only sub-harness (``build_subharness`` documents the empty case).
        """
        subs = dict(existing or {})
        for m in manifest.members:
            if m.role in subs or m.kind != "agent":
                continue
            subs[m.role] = build_subharness(
                m.role,
                owner_organization_id=org,
                body=(m.subgoal or f"You are the {m.role}. Complete your part of the objective."),
                tools=list(m.tools),
            ).model_dump(mode="json")
        return subs

    @staticmethod
    def _known_agent_ids(row: EngineTeamDraft | None) -> set[uuid.UUID]:
        """The agent ids the STORED draft already points at — the only ones a write may reuse."""
        known: set[uuid.UUID] = set()
        members = (getattr(row, "manifest", None) or {}).get("members") if row else None
        for member in members if isinstance(members, list) else []:
            try:
                known.add(uuid.UUID(str(member.get("manifest_ref"))))
            except (AttributeError, TypeError, ValueError):
                continue
        return known

    @staticmethod
    def _agent_id(manifest_ref: str | None, known: set[uuid.UUID]) -> uuid.UUID:
        """The registry id to file a member's generated agent under (#695, ADR-050).

        Taken from the ``manifest_ref`` on the manifest BEING WRITTEN when that parses as a UUID
        **and the stored draft already points at it**, and minted fresh otherwise. The first two
        halves look contradictory and are the same rule: a draft already carrying registry ids is
        REFRESHED in place (an edit updates the agents the user already has rather than minting a
        second set beside them), while a legacy ``org:compiled/<role>@1`` — which resolved to
        nothing — has no id to reuse.

        The ``known`` check is the third half, and it is a security bound rather than a nicety.
        ``manifest_ref`` arrives on the REQUEST BODY and filing PUTs an existing row in place, so
        without it a user could name a colleague's agent id in a draft they are creating and
        silently overwrite that agent's descriptor. Cross-org was already closed — the registry
        read and the write are both org-scoped under RLS — but same-org was not. An id the caller's
        own stored draft does not already reference is therefore never reused: the write mints a
        fresh agent instead, which is the fail-closed direction (a spare row, never a clobbered
        one). A create has no stored draft at all, so every id on it is minted.
        """
        try:
            candidate = uuid.UUID(str(manifest_ref))
        except (TypeError, ValueError):
            return uuid.uuid4()
        return candidate if candidate in known else uuid.uuid4()

    async def _file_agents(
        self,
        manifest_doc: dict[str, Any],
        team: OHMManifest,
        org: uuid.UUID,
        *,
        existing: dict[str, Any] | None = None,
        stored: EngineTeamDraft | None = None,
    ) -> dict[str, Any]:
        """Build each agent member's sub-harness, FILE it in the registry, and point the member at
        the returned id. Returns what the draft should store under ``sub_harnesses``.

        #695: a compiled member and a console-built agent are the same object, and only the console
        one was ever filed. The compiler stamped ``org:compiled/<role>@1``, which resolves to
        nothing, and shipped the generated manifests inline — so the agents were unlistable,
        uneditable, unbindable, and died with the run.

        ADR-050 D3, one source of truth: once the agents are filed the draft stops carrying them
        inline. Keeping both would mean two copies of every agent, and editing the filed one would
        silently not affect the team — which makes the reuse this exists for cosmetic. The RUN
        record keeps its own snapshot instead (``TeamRunService.create``).

        Fail-closed and NOT fail-soft: a registry failure fails the whole draft write. A
        half-registered draft is worse than one that was not saved — the team would point at one
        filed agent and one dangling reference, and that would only surface at the next run.

        Every descriptor is put on the graph substrate BEFORE it is filed, whatever route it came
        in by. ``build_subharness`` already does that for one the platform synthesizes, keyed on the
        declared tool name — but a caller-supplied ``sub_harnesses`` entry is kept verbatim, and the
        capability-absence and substrate gates both read ``members[].tools``, never the supplied
        descriptor's own ``capabilities[].ref``. A member declaring ``graph-ingest`` could therefore
        arrive with ``{"ref": "core/write@1", "binding": "graph-ingest"}`` underneath it: a tmp
        sandbox behind a clean, ceiling-passing name, verdict green. That is run ``fe548aac``
        exactly — except the old version died with the run and a FILED one survives it, which is
        what the tests PR meant by "registering agents that still declared file tools would put the
        wrong thing in the library permanently". The likeliest way in is not an attacker but the
        console forwarding a pre-#695 draft's stored ``sub_harnesses`` on a save, which is also
        Amendment 2's healing case seen from the other side: one rule at one place serves both.

        With no registry wired there is nowhere to file anything, so the old inline shape is kept
        rather than a draft being written with references that resolve to nothing.

        Concurrency: two simultaneous ``from-run`` saves for one run both clear the idempotency
        fast-path and both file a fresh set of agents, and only one wins the partial-unique insert.
        The loser's agents stay in the library with nothing pointing at them — spare rows, never a
        wrong one. Narrow enough to leave, and named here so it is not rediscovered as a mystery.
        """
        subs = self._synthesize_subs(team, org, existing=existing)
        if self._registry is None:
            return subs
        known = self._known_agent_ids(stored)
        raw_members = manifest_doc.get("members")
        by_role: dict[str, dict[str, Any]] = {}
        if isinstance(raw_members, list):
            for raw in raw_members:
                if isinstance(raw, dict) and isinstance(raw.get("role"), str):
                    by_role[raw["role"]] = raw
        for member in team.members:
            # a kind:human member is a GATE, not an agent: it has no sub-harness to build and
            # nothing to put in the library — filing one would place a person on /app/agents.
            if member.kind != "agent":
                continue
            descriptor = subs.get(member.role)
            if descriptor is None:
                continue
            # the substrate correction, applied to what is ACTUALLY about to be filed
            descriptor = remap_capability_refs(descriptor)
            subs[member.role] = descriptor
            agent_id = self._agent_id(member.manifest_ref, known)
            metadata = descriptor.get("metadata")
            if not isinstance(metadata, dict):  # defensive — build_subharness always emits one
                continue
            metadata["id"] = str(agent_id)
            try:
                filed = await self._registry.upsert_harness(descriptor, descriptor_id=agent_id)
            except RegistryClientError as exc:
                raise TeamRunError(
                    f"the capability registry could not file the '{member.role}' agent,"
                    " so the team was not saved",
                    502,
                    error_type="agent_registration_failed",
                ) from exc
            raw = by_role.get(member.role)
            if raw is not None:
                raw["manifest_ref"] = str(filed)
        return {}

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
        # #695 Amendment 2 names four entry points and this is one of them: ``_synthesize_subs``
        # fills only the roles the caller left out, so a caller's own sub-harness still wins and an
        # omitted member is no longer bodiless.
        subs = await self._file_agents(manifest, team, org, existing=sub_harnesses)
        with org_scope(org):
            row = await self._drafts.create(
                organisation_id=org,
                user_id=principal.principal_id,
                name=name,
                manifest=manifest,
                sub_harnesses=subs,
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
        # ADR-006 fail-closed tenancy: a cross-org draft id is absent, and the miss must be found
        # BEFORE anything is filed — otherwise a tenancy miss would copy one org's agent into
        # another org's library on its way to the 404.
        stored = await self._get_or_404(draft_id, org)
        team = self._load_team(manifest)
        verdict = await self._verdict(manifest, team)
        # #694 Amendment 2, the healing seam. Unlike ``create_from_run`` — which refuses a blocked
        # team outright — ``replace`` PERSISTS a blocked draft and returns the verdict beside it, so
        # the user can refine until the strip is green. A path that refuses cannot heal, so this is
        # where a draft compiled before the substrate fix has its file refs re-synthesized onto the
        # graph. Its stored manifest then describes what it actually uses, at all times.
        subs = await self._file_agents(manifest, team, org, existing=sub_harnesses, stored=stored)
        with org_scope(org):
            row = await self._drafts.replace(
                draft_id, org, name=name, manifest=manifest, sub_harnesses=subs
            )
        if row is None:  # deleted from under us between the read and the write
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
        # Carry through everything the drafter declared BESIDE members[]. assemble_and_report
        # rebuilds the stored manifest from members alone, so each of these is dropped here unless
        # it is threaded — and each drop has a cost:
        #   task_input (#714) — the console GO path has nowhere to put the user's task;
        #   budget      — nothing bounds a member's tool loop. The #714 proof failed exactly here:
        #                 the drafter declared max_tool_calls_per_member 50, the rebuild dropped
        #                 it, and the Reviewer re-read the PR's comments to 219,124 tokens;
        #   governance  — the team ships with no policy set and no redact patterns, against #596's
        #                 governed-by-default promise. The quietest of the three, and the worst;
        #   orchestration — the drafted style / success_criteria (the #477 flow gate) is lost.
        # The gate above already ran, so a malformed block never reaches this line.
        result = assemble_and_report(
            draft_name,
            members,
            owner_organization_id=org,
            shape="compiled",
            task_input=compiled.get("task_input"),
            governance=compiled.get("governance"),
            budget=compiled.get("budget"),
            orchestration=_maybe_orchestration(compiled.get("orchestration")),
        )
        if result.manifest is None:  # defensive — the gate above already proved assemblable
            raise TeamRunError(
                "the compiled team is not assemblable: " + "; ".join(result.report.blocking),
                422,
                error_type="compiled_team_blocked",
            )
        # #695: filing happens BEFORE the row is written, so a registry failure leaves no
        # half-registered draft behind. ``from-run`` IS the explicit save — a compile the user
        # abandons never becomes a draft and registers nothing.
        manifest_doc = result.manifest.model_dump(mode="json")
        subs = await self._file_agents(manifest_doc, result.manifest, org)
        with org_scope(org):
            row, created = await self._drafts.create_from_run(
                organisation_id=org,
                user_id=principal.principal_id,
                name=draft_name,
                manifest=manifest_doc,
                sub_harnesses=subs,
                team_run_id=team_run_id,
            )
        if not created:  # lost the partial-unique race — return the winner's draft + its verdict
            return row, await self._verdict(row.manifest, self._load_team(row.manifest)), False
        return row, DraftVerdict.from_validate(gate), True

    @staticmethod
    def _peel_json(raw: Any, *, who: str, error_type: str) -> dict[str, Any]:
        """A member result is ``{"output": <text>, "status": ...}``; older shapes return the text
        directly. The member's answer object is peeled out of the surrounding prose (and out of a
        trailing #641 grounding receipt) — anything else is a curated 422, never a 500."""
        if raw is None:
            raise TeamRunError(f"{who} produced no output", 422, error_type=error_type)
        text = raw.get("output") if isinstance(raw, dict) else raw
        if not isinstance(text, str) or not text.strip():
            raise TeamRunError(f"{who} produced no text output", 422, error_type=error_type)
        if "{" not in text:
            raise TeamRunError(f"{who} emitted no JSON object", 422, error_type=error_type)
        parsed = _first_json_object(text)
        if parsed is None:
            raise TeamRunError(f"{who} emitted malformed JSON", 422, error_type=error_type)
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
        # #695 R2's other half: each id comes from the STORED member's ``manifest_ref``, so a
        # refine REFRESHES the agents the user already has and files only the genuinely new one.
        manifest_doc = result.manifest.model_dump(mode="json")
        subs = await self._file_agents(
            manifest_doc, result.manifest, org, existing=dict(row.sub_harnesses), stored=row
        )
        with org_scope(org):
            updated = await self._drafts.update_documents(
                row.id,
                org,
                manifest=manifest_doc,
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
        # #713: the op-drafter picks tools too (an `add_member` op names them), and it reads this
        # text directly — no relay — so it gets the described catalog rather than bare slugs.
        subgoal = (
            f"CURRENT TEAM MANIFEST:\n{json.dumps(manifest)}\n\n"
            f"SURVEYED CATALOG: {json.dumps(await self._described_catalog())}\n\n"
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
