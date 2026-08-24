"""The validation desk's intake read-back (services layer) — #866.

The desk's first step takes a founder's idea as a paragraph of free text, and until this service
existed nothing on the platform read it before the run started. So the three questions under it
were hardcoded — "who is the target customer", "what will they pay", "what stage are you at" —
exactly the generic questions the design argues against, and there was no restatement at all.

Four decisions shape this, each one ruled on #866:

- **The floor refuses without spending anything.** Under ``IDEA_MIN_CHARS``, no model is called at
  all, so the refusal is instant and reproducible. Vagueness is never the model's judgement.
- **No platform fallback model.** The founder's own bound models answer the call. With none bound
  it refuses rather than silently borrowing one — that is what ``MODEL_NOT_CONNECTED`` is for.
- **A slow model is not a failure.** The reader is a real LLM run, polled up to a budget that sits
  under the gateway's upstream read timeout; past it the caller gets the run id back (a 202 at the
  route) and re-calls to collect. Same shape as refine-nl (#595).
- **One member, submitted through the same path as every other run.** No second way to call a
  model, no bypass of the run's provenance and budget machinery.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.compiler.prompts import INTAKE_READER_PROMPT
from oraclous_ohm.import_.mapping import build_subharness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

from oraclous_execution_engine_service.domain.intake_readback import (
    Question,
    ReadbackShapeError,
    Span,
    idea_meets_floor,
    parse_readback,
)
from oraclous_execution_engine_service.domain.model_answer import first_json_object
from oraclous_execution_engine_service.services.team_run_service import TeamRunService

#: The reader team's name, used to prove a collect token names a read-back run and not some other
#: run of the same organisation.
READER_TEAM_NAME = "intake-reader"
READER_ROLE = "reader"

_TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "FAILED", "REJECTED", "COST_BUDGET"})


class IntakeReadbackError(Exception):
    """A client-facing read-back failure.

    ``error_code`` is a taxonomy value the route puts in the body. It matters because the gateway
    drains an upstream error body rather than relaying it, so an allow-listed code is the only
    thing that reaches the founder's browser (#866). ``error_type`` is the leak-safe machine token
    for the ordinary structured-422 path.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 422,
        *,
        error_code: str | None = None,
        error_type: str = "readback_invalid",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type


@dataclass(frozen=True)
class Readback:
    """The idea, read back: ordered spans plus at most three questions."""

    restatement: list[Span]
    questions: list[Question]
    readback_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PendingReadback:
    """The reader outran the poll budget. The caller re-calls with this id to collect."""

    readback_run_id: uuid.UUID


class IntakeReadbackService:
    def __init__(
        self,
        *,
        team_runs: TeamRunService,
        readback_poll_seconds: float = 25.0,
        readback_poll_interval_seconds: float = 2.0,
    ) -> None:
        self._team_runs = team_runs
        self._poll_budget = readback_poll_seconds
        self._poll_interval = readback_poll_interval_seconds

    async def readback(
        self,
        principal: Principal,
        *,
        idea: str | None = None,
        models: list[dict[str, Any]] | None = None,
        readback_run_id: uuid.UUID | None = None,
    ) -> Readback | PendingReadback:
        """Read the idea back, or collect a read that was still running.

        Either ``idea`` (+ the caller's bound ``models``) to start a read, or ``readback_run_id``
        from a prior 202 to collect one.
        """
        org = self._org(principal)
        if readback_run_id is None:
            readback_run_id = (await self._submit(principal, org, idea=idea, models=models)).id
        settled = await self._await_reader(readback_run_id, principal)
        if settled is None:
            return PendingReadback(readback_run_id)
        return self._peel(settled, readback_run_id)

    # ── the two refusals that never reach a model ────────────────────────────

    @staticmethod
    def _org(principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise IntakeReadbackError(
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
        idea: str | None,
        models: list[dict[str, Any]] | None,
    ) -> Any:
        if idea is None or not idea.strip():
            raise IntakeReadbackError(
                "the read-back needs an idea (or a readback_run_id to collect)",
                422,
                error_type="missing_idea",
            )
        # The floor runs FIRST and costs nothing. A founder who typed one line is told so
        # immediately, rather than after a model round trip that was never going to help.
        if not idea_meets_floor(idea):
            raise IntakeReadbackError(
                "the idea is below the length floor",
                422,
                error_code="IDEA_TOO_VAGUE",
                error_type="idea_too_vague",
            )
        if not models:
            # No platform fallback on this path, by ruling. Borrowing one would bill an
            # unconnected founder and read their idea with a model they never chose.
            raise IntakeReadbackError(
                "no model is connected",
                409,
                error_code="MODEL_NOT_CONNECTED",
                error_type="model_not_connected",
            )
        team = OHMManifest(
            ohm_version="1.1",
            metadata=OHMMetadata(
                id=uuid.uuid4(),
                name=READER_TEAM_NAME,
                owner_organization_id=org,
                kind="team",
            ),
            members=[
                OHMMember(
                    role=READER_ROLE,
                    kind="agent",
                    manifest_ref="org:intake/reader@1",
                    # The reader gets the founder's words directly, unsummarised — a relayed
                    # paraphrase is exactly the inference this endpoint exists to make visible.
                    subgoal=f"FOUNDER'S IDEA:\n{idea.strip()}",
                )
            ],
            runtime=OHMRuntime(entrypoint=READER_ROLE),
        )
        doc = team.model_dump(mode="json")
        doc["models"] = models
        sub = build_subharness(
            READER_ROLE, owner_organization_id=org, body=INTAKE_READER_PROMPT, tools=[]
        ).model_dump(mode="json")
        sub["models"] = models
        return await self._team_runs.create(
            principal, manifest=doc, sub_harnesses={READER_ROLE: sub}, gate_decisions={}
        )

    # ── the poll ─────────────────────────────────────────────────────────────

    async def _await_reader(self, run_id: uuid.UUID, principal: Principal) -> Any | None:
        """Poll the reader run up to the budget. ``None`` = still driving (caller 202s)."""
        deadline = time.monotonic() + self._poll_budget
        checked_shape = False
        while True:
            run = await self._team_runs.get(run_id, principal)  # 404 if not this org's run
            if not checked_shape:
                # Fail-closed on run identity, on the FIRST read before any budget burns: a
                # collect token is only redeemable against a read-back run. Any other same-org run
                # id would otherwise buy a full poll and a peel that reads someone else's output
                # as a restatement of the founder's idea.
                name = ((run.manifest or {}).get("metadata") or {}).get("name")
                if name != READER_TEAM_NAME:
                    raise IntakeReadbackError(
                        "readback_run_id does not name a read-back run",
                        422,
                        error_type="not_a_readback_run",
                    )
                checked_shape = True
            if run.state in _TERMINAL_RUN_STATES:
                if run.state != "SUCCEEDED":
                    raise IntakeReadbackError(
                        f"the read-back run did not succeed (state {run.state})",
                        422,
                        error_type="readback_failed",
                    )
                return run
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll_interval)

    # ── the answer ───────────────────────────────────────────────────────────

    @staticmethod
    def _peel(run: Any, run_id: uuid.UUID) -> Readback:
        """Peel the reader's JSON answer and hold it to the endpoint's contract.

        A model answering in prose, or inventing a third ``source``, is an expected outcome of
        asking a model — a curated 422, never a 500.
        """
        raw = (run.results or {}).get(READER_ROLE)
        text = raw.get("output") if isinstance(raw, dict) else raw
        if not isinstance(text, str) or not text.strip():
            raise IntakeReadbackError(
                "the reader produced no output", 422, error_type="reader_output_unparseable"
            )
        parsed = first_json_object(text)
        if parsed is None:
            raise IntakeReadbackError(
                "the reader emitted no usable JSON object",
                422,
                error_type="reader_output_unparseable",
            )
        try:
            spans, questions = parse_readback(parsed)
        except ReadbackShapeError as exc:
            raise IntakeReadbackError(
                "the reader's answer does not fit the read-back contract",
                422,
                error_type="reader_output_unparseable",
            ) from exc
        return Readback(restatement=spans, questions=questions, readback_run_id=run_id)
