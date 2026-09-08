"""The service that asks a model what a converted app's form should be (services layer, #938).

A team declares exactly one input, so a converted app has no field names to label. The owner ruled
that a model invents them, reading the team's own description and the request text the run was
actually started with, and the person edits them before the app is saved. This is deliberately the
same shape as the shipped intake read-back (``IntakeReadbackService``, #866) — the only precedent
in this service for a model-backed console step, and a second shape would be a second thing to
reason about:

- **The refusals cost nothing.** No organisation, no model bound, a run that is not the caller's, a
  run that did not succeed — each is settled before a single token is spent.
- **No platform fallback model.** The caller's own bound model does the drafting, for the same
  reason the read-back has none: borrowing one would bill a person who connected nothing.
- **A slow model is not a failure.** The drafter is a real run, polled up to a budget under the
  gateway's read timeout; past it the caller gets a token back and re-calls to collect.
- **A token is only redeemable against a drafting run.** Handing in any other run of the same
  organisation is a curated refusal on the first read, not a burned budget and a nonsense form.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.compiler.prompts import APP_FORM_DRAFTER_PROMPT
from oraclous_ohm.import_.mapping import build_subharness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.app_form import FormShapeError, parse_form_draft
from oraclous_execution_engine_service.domain.model_answer import first_json_object
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.compiler_run_service import (
    validate_model_bindings,
)
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
)

#: The drafting team's name, used to prove a collect token names a drafting run and not some other
#: run of the same organisation.
DRAFTER_TEAM_NAME = "app-form-drafter"
DRAFTER_ROLE = "drafter"

#: The label the drafting team's subgoal puts in front of the request text. #963's mechanical check
#: needs that request back at PEEL time, and the peel may happen on a later call (the collect path,
#: where the original run id was never supplied), so it is recovered from the drafting run's own
#: manifest rather than carried in memory. The subgoal is built two functions below; the two must
#: agree, which is why the marker is a constant rather than a literal in each place.
_REQUEST_MARKER = "REQUEST THIS RUN WAS STARTED WITH:\n"

_TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "FAILED", "REJECTED", "COST_BUDGET"})


class AppFormDraftError(Exception):
    """A client-facing drafting failure.

    ``error_code`` is a taxonomy value the route puts in the body — the one thing that survives the
    gateway's error-body drain (CLAUDE.md's gateway error wall). ``error_type`` is the leak-safe
    machine token for the ordinary structured-422 path.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 422,
        *,
        error_code: str | None = None,
        error_type: str = "form_draft_invalid",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type


@dataclass(frozen=True)
class DraftedForm:
    """The form a model proposed, held to ``domain.app_form``'s contract."""

    fields: list[Any]
    form_draft_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PendingFormDraft:
    """The drafter outran the poll budget. The caller re-calls with this id to collect."""

    form_draft_run_id: uuid.UUID


class AppFormDraftService:
    def __init__(
        self,
        *,
        team_runs: TeamRunService,
        team_run_repository: TeamRunRepository,
        draft_poll_seconds: float = 25.0,
        draft_poll_interval_seconds: float = 2.0,
    ) -> None:
        self._team_runs = team_runs
        self._team_run_repository = team_run_repository
        self._poll_budget = draft_poll_seconds
        self._poll_interval = draft_poll_interval_seconds

    async def suggest(
        self,
        principal: Principal,
        *,
        team_run_id: uuid.UUID | None = None,
        models: list[dict[str, Any]] | None = None,
        form_draft_run_id: uuid.UUID | None = None,
    ) -> DraftedForm | PendingFormDraft:
        """Draft a form from ``team_run_id`` (+ the caller's bound ``models``), or collect a draft
        that was still running (``form_draft_run_id`` from a prior pending outcome)."""
        org = self._org(principal)
        run_id = form_draft_run_id
        if run_id is None:
            created = await self._submit(principal, org, team_run_id=team_run_id, models=models)
            run_id = created.id
        settled = await self._await_drafter(run_id, principal)
        if settled is None:
            return PendingFormDraft(run_id)
        return self._peel(settled, run_id)

    # ── the refusals that never reach a model ────────────────────────────────

    @staticmethod
    def _org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise AppFormDraftError(
                "authenticated principal has no organisation scope",
                403,
                error_type="no_organisation",
            )
        return principal.organisation_id

    async def _submit(
        self,
        principal: Principal,
        org: uuid.UUID,
        *,
        team_run_id: uuid.UUID | None,
        models: list[dict[str, Any]] | None,
    ) -> Any:
        if team_run_id is None:
            raise AppFormDraftError(
                "a form draft needs a team_run_id (or a form_draft_run_id to collect)",
                422,
                error_type="missing_team_run_id",
            )
        if not models:
            # No platform fallback on this path, same ruling as the intake read-back: borrowing one
            # would bill a person who connected nothing with a model they never chose.
            raise AppFormDraftError(
                "no model is connected",
                409,
                error_code="MODEL_NOT_CONNECTED",
                error_type="model_not_connected",
            )
        with org_scope(org):
            run = await self._team_run_repository.get(team_run_id, org)
        if run is None:
            # Not a 403 — confirming a run exists but belongs to another organisation is an
            # enumeration the engine's other reads already refuse to make.
            raise AppFormDraftError("team run not found", 404, error_type="team_run_not_found")
        if run.state != "SUCCEEDED":
            raise AppFormDraftError(
                f"an app is made from a run its author watched work (state {run.state})",
                422,
                error_type="run_not_succeeded",
            )
        try:
            bound = validate_model_bindings(models, who="the app form drafter")
        except TeamRunError as exc:
            raise AppFormDraftError(str(exc), exc.status_code, error_type=exc.error_type) from exc

        task_input = (run.manifest or {}).get("task_input") or {}
        task_key = task_input.get("key")
        request_text = ((run.inputs or {}).get(task_key) if task_key else None) or ""
        request_text = request_text.strip()
        description = (task_input.get("description") or "").strip()

        team = OHMManifest(
            ohm_version="1.1",
            metadata=OHMMetadata(
                id=uuid.uuid4(),
                name=DRAFTER_TEAM_NAME,
                owner_organization_id=org,
                kind="team",
            ),
            members=[
                OHMMember(
                    role=DRAFTER_ROLE,
                    kind="agent",
                    manifest_ref="org:app-form/drafter@1",
                    # Both the team's own description AND the request this run was actually
                    # started with, unsummarised — the request is what makes the invented fields
                    # SPECIFIC rather than generic, and it is the one input the fold exists for.
                    subgoal=f"TEAM DESCRIPTION:\n{description}\n\n{_REQUEST_MARKER}{request_text}",
                )
            ],
            runtime=OHMRuntime(entrypoint=DRAFTER_ROLE),
        )
        doc = team.model_dump(mode="json")
        doc["models"] = bound
        sub = build_subharness(
            DRAFTER_ROLE, owner_organization_id=org, body=APP_FORM_DRAFTER_PROMPT, tools=[]
        ).model_dump(mode="json")
        sub["models"] = bound
        return await self._team_runs.create(
            principal, manifest=doc, sub_harnesses={DRAFTER_ROLE: sub}, gate_decisions={}
        )

    # ── the poll ─────────────────────────────────────────────────────────────

    async def _await_drafter(self, run_id: uuid.UUID, principal: Principal) -> Any | None:
        """Poll the drafting run up to the budget. ``None`` = still driving (caller re-collects)."""
        deadline = time.monotonic() + self._poll_budget
        checked_shape = False
        while True:
            try:
                run = await self._team_runs.get(run_id, principal)  # 404 if not this org's run
            except TeamRunError as exc:
                raise AppFormDraftError(
                    str(exc), exc.status_code, error_type=exc.error_type
                ) from exc
            if not checked_shape:
                # Fail-closed on run identity, on the FIRST read before any budget burns: a collect
                # token is only redeemable against a drafting run. Any other same-org run id would
                # otherwise buy a full poll and have its output read as a proposed form.
                name = ((run.manifest or {}).get("metadata") or {}).get("name")
                if name != DRAFTER_TEAM_NAME:
                    raise AppFormDraftError(
                        "form_draft_run_id does not name a form-drafting run",
                        422,
                        error_type="not_a_form_draft_run",
                    )
                checked_shape = True
            if run.state in _TERMINAL_RUN_STATES:
                if run.state != "SUCCEEDED":
                    raise AppFormDraftError(
                        f"the drafting run did not succeed (state {run.state})",
                        422,
                        error_type="draft_failed",
                    )
                return run
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll_interval)

    # ── the answer ───────────────────────────────────────────────────────────

    @staticmethod
    def _request_text(run: Any) -> str:
        """The request the ORIGINAL run was started with, recovered from the drafting run itself.

        #963's check compares a website field's example against what the person actually wrote, and
        the peel is the only place that has the drafted form — but not, on the collect path, the
        original run id. The drafting team's own subgoal carries the request verbatim (it is what
        makes the invented fields specific rather than generic), so it is read back from there.

        Returns ``""`` when the marker is absent, and an empty request means the example is kept as
        given: an absent comparison is no evidence the example was invented.
        """
        for member in (run.manifest or {}).get("members") or []:
            subgoal = member.get("subgoal") if isinstance(member, dict) else None
            if isinstance(subgoal, str) and _REQUEST_MARKER in subgoal:
                return subgoal.split(_REQUEST_MARKER, 1)[1].strip()
        return ""

    @classmethod
    def _peel(cls, run: Any, run_id: uuid.UUID) -> DraftedForm:
        """Peel the drafter's JSON answer and hold it to ``domain.app_form``'s contract.

        A model answering in prose is an expected outcome of asking a model — a curated 422, never
        a 500.

        #963: the run's own request text goes in with the draft, so a website field's example can be
        checked against what the person really wrote. Only a field that DECLARED itself a site
        restriction is checked (#961 ruling 4); everything else is untouched.
        """
        raw = (run.results or {}).get(DRAFTER_ROLE)
        text = raw.get("output") if isinstance(raw, dict) else raw
        if not isinstance(text, str) or not text.strip():
            raise AppFormDraftError(
                "the drafter produced no output", 422, error_type="drafter_output_unparseable"
            )
        parsed = first_json_object(text)
        if parsed is None:
            raise AppFormDraftError(
                "the drafter emitted no usable JSON object",
                422,
                error_type="drafter_output_unparseable",
            )
        try:
            fields = parse_form_draft(parsed, request_text=cls._request_text(run))
        except FormShapeError as exc:
            raise AppFormDraftError(
                "the drafter's answer does not fit the form contract",
                422,
                error_type="drafter_output_unparseable",
            ) from exc
        return DraftedForm(fields=fields, form_draft_run_id=run_id)
