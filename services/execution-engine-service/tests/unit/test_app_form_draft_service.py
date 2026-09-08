"""#938 — the service that asks a model what an app's form should be.

A team declares exactly one input, so a converted app has no field names to label. The owner ruled
that a model invents them, reading the team's own description and the request text the run was
actually started with. This module pins what that service does BEFORE and AROUND the model, which is
everything a fast gate can check.

It is deliberately the same shape as the shipped intake read-back (#866), because that is the only
precedent in this service for a model-backed console step and a second shape would be a second thing
to reason about:

- **The refusals cost nothing.** No organisation, no model bound, a run that is not the caller's,
  a run that did not succeed — each is settled before a single token is spent, and each test asserts
  the submit seam was never touched.
- **No platform fallback model.** The caller's own bound model does the drafting. With none bound it
  refuses rather than silently borrowing one.
- **A slow model is not a failure.** The drafter is a real run, polled up to a budget under
  the gateway's read timeout; past it the caller gets a token back and re-calls to collect.
- **A token is only redeemable against a drafting run.** Handing in any other run of the same
  organisation is a curated refusal on the first read, not a burned budget and a nonsense form.

The model run is faked at the ``TeamRunService`` seam, and only there. The REAL model path is the
deployed end-to-end run's job — a faked model is never a proof of done (RULE 8), which is exactly
why the drafting leg of ``tests/e2e/test_app_from_run_gateway_e2e.py`` requires a real key.

Added at the Tests Review gate: without these, every refusal above was provable only by an
end-to-end test that skips when no key is present, so the fast gate had no coverage of them at all.

Seam imported FUNCTION-LOCALLY (``.claude/rules/tests-seam-imports.md``) — RED until the impl lands.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_OTHER_ORG = uuid.uuid4()
_USER = uuid.uuid4()

#: The request the source run was started with. Everything the drafter proposes is read out of THIS.
_REQUEST = (
    "Write a competitor brief on Acme Cloud, focused on their pricing move last month. "
    "Keep it quick — one short paragraph."
)

_MODELS = [
    {
        "role": "primary",
        "binding": "openrouter/x",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": "c1"},
    }
]

_ANSWER = json.dumps(
    {
        "fields": [
            {
                "name": "Competitor",
                "hint": "The company this brief is about.",
                "type": "short_text",
                "example": "Acme Cloud",
                "required": True,
            },
            {
                "name": "Focus",
                "hint": "What angle to take.",
                "type": "short_text",
                "example": "their pricing move last month",
                "required": True,
            },
        ]
    }
)

#: The name the drafting team runs under, so a collect token can be checked against it.
_DRAFTER_TEAM_NAME = "app-form-drafter"
_DRAFTER_ROLE = "drafter"


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


def _source_manifest() -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {"id": str(uuid.uuid4()), "name": "competitor-brief", "kind": "team"},
        "task_input": {
            "required": True,
            "key": "task",
            "description": "The competitor to cover and the angle to take.",
        },
        "members": [{"role": "writer", "kind": "agent", "manifest_ref": "org:brief/writer@1"}],
        "runtime": {"entrypoint": "writer"},
    }


class _RunRow:
    def __init__(
        self,
        state: str,
        *,
        results: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        organisation_id: uuid.UUID = _ORG,
    ) -> None:
        self.id = uuid.uuid4()
        self.state = state
        self.results = results
        self.manifest = manifest or {"metadata": {"name": _DRAFTER_TEAM_NAME}}
        self.inputs = inputs
        self.organisation_id = organisation_id


class _FakeTeamRuns:
    """The ``TeamRunService`` seam the drafter consumes: create (submit) + get (poll)."""

    def __init__(self, *, drafter_output: str = _ANSWER, settles: bool = True) -> None:
        self.runs: dict[uuid.UUID, _RunRow] = {}
        self.created: list[dict[str, Any]] = []
        self._output = drafter_output
        self._settles = settles

    def seed(self, row: _RunRow) -> _RunRow:
        self.runs[row.id] = row
        return row

    async def create(self, principal: Principal, **kw: Any) -> _RunRow:
        self.created.append(kw)
        row = _RunRow(
            "SUCCEEDED" if self._settles else "RUNNING",
            results={_DRAFTER_ROLE: {"output": self._output, "status": "SUCCEEDED"}}
            if self._settles
            else {},
        )
        return self.seed(row)

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _RunRow:
        row = self.runs.get(run_id)
        if row is None:
            from oraclous_execution_engine_service.services.team_run_service import TeamRunError

            raise TeamRunError("team run not found", 404)
        return row


class _FakeRunRepository:
    """The repository the SOURCE run is read through — org-scoped, so a cross-org id is simply
    absent rather than a row with a refusal attached to it."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, uuid.UUID], _RunRow] = {}

    def seed(self, row: _RunRow) -> _RunRow:
        self.rows[(row.id, row.organisation_id)] = row
        return row

    async def get(self, run_id: uuid.UUID, organisation_id: uuid.UUID) -> _RunRow | None:
        return self.rows.get((run_id, organisation_id))


def _service(team_runs: _FakeTeamRuns | None = None, repo: _FakeRunRepository | None = None):  # noqa: ANN202 — the seam ships its own type
    from oraclous_execution_engine_service.services.app_form_draft_service import (
        AppFormDraftService,
    )

    team_runs = team_runs or _FakeTeamRuns()
    repo = repo or _FakeRunRepository()
    svc = AppFormDraftService(
        team_runs=team_runs,  # type: ignore[arg-type] — duck-typed seam in unit tests
        team_run_repository=repo,  # type: ignore[arg-type]
        draft_poll_seconds=0.2,
        draft_poll_interval_seconds=0.01,
    )
    return svc, team_runs, repo


def _error() -> type[Exception]:
    from oraclous_execution_engine_service.services.app_form_draft_service import AppFormDraftError

    return AppFormDraftError


def _pending() -> type:
    from oraclous_execution_engine_service.services.app_form_draft_service import PendingFormDraft

    return PendingFormDraft


def _source(
    repo: _FakeRunRepository, *, state: str = "SUCCEEDED", org: uuid.UUID = _ORG
) -> _RunRow:
    return repo.seed(
        _RunRow(
            state,
            manifest=_source_manifest(),
            inputs={"task": _REQUEST},
            organisation_id=org,
        )
    )


# ── refusals that never reach a model ────────────────────────────────────────


async def test_a_principal_with_no_organisation_is_refused() -> None:
    """Fail-closed tenancy (ADR-006). There is no organisation whose run this could be."""
    svc, team_runs, repo = _service()
    run = _source(repo)

    with pytest.raises(_error()) as caught:
        await svc.suggest(_principal(None), team_run_id=run.id, models=_MODELS)

    assert getattr(caught.value, "status_code", None) == 403
    assert team_runs.created == []


async def test_no_model_bound_refuses_rather_than_borrowing_one() -> None:
    """There is no platform fallback on this path, for the same reason the intake read-back has
    none: borrowing a model would bill a person who connected nothing and read their request with
    a model they never chose."""
    svc, team_runs, repo = _service()
    run = _source(repo)

    with pytest.raises(_error()) as caught:
        await svc.suggest(_principal(), team_run_id=run.id, models=[])

    assert getattr(caught.value, "status_code", None) == 409
    assert getattr(caught.value, "error_code", None) == "MODEL_NOT_CONNECTED"
    assert team_runs.created == []


async def test_a_run_belonging_to_another_organisation_is_a_404() -> None:
    """Not a 403. Confirming that a run exists but is not yours is an enumeration the engine's
    other reads already refuse to make."""
    svc, team_runs, repo = _service()
    run = _source(repo, org=_OTHER_ORG)

    with pytest.raises(_error()) as caught:
        await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    assert getattr(caught.value, "status_code", None) == 404
    assert team_runs.created == []


async def test_a_run_that_did_not_succeed_is_refused_before_the_model() -> None:
    """An app is made from a run its author watched work, so drafting a form for a failed one would
    spend a model call on something that can never become an app."""
    svc, team_runs, repo = _service()
    run = _source(repo, state="FAILED")

    with pytest.raises(_error()) as caught:
        await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    assert getattr(caught.value, "status_code", None) == 422
    assert team_runs.created == []


# ── what the drafter is actually handed ──────────────────────────────────────


async def test_the_drafter_is_handed_the_request_the_run_was_started_with() -> None:
    """The ruling, at the only seam that can check it cheaply. A drafter given the team's
    description alone proposes generic fields — the person's own request is what makes them
    specific, and it is the one input that must not be summarised on the way in."""
    svc, team_runs, repo = _service()
    run = _source(repo)

    await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    # ``ensure_ascii=False``: the request below carries an em dash, and the default escapes it
    # to ``\\u2014`` — so this assert could never pass for an implementation that forwards the
    # text verbatim, which is exactly what the ruling requires.
    submitted = json.dumps(team_runs.created[-1], ensure_ascii=False)
    assert _REQUEST in submitted, "the drafter never saw the request it is meant to read"
    assert "The competitor to cover and the angle to take." in submitted, (
        "the drafter never saw what the team says it does"
    )


async def test_the_drafter_is_told_to_ask_for_a_website_address_not_a_name() -> None:
    """#953 — at the seam, not at the constant.

    The rule itself is pinned in ``packages/ohm``'s prompt tests. This asserts it survives the trip
    to the model: the document actually submitted for the drafting run has to carry it. A prompt
    updated in one place and assembled from another would pass there and fail a person here.

    Deliberately written without importing the prompt, so it is a claim about the model-facing
    document rather than an echo of a constant. Neither word appears anywhere else in this
    fixture's manifest or request text.
    """
    svc, team_runs, repo = _service()
    run = _source(repo)

    await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    submitted = json.dumps(team_runs.created[-1], ensure_ascii=False).lower()
    assert "address" in submitted, (
        "nothing in the document handed to the drafter asks for a website's address, so it is "
        "free to invent a field asking for a publication name — a value a search cannot honour"
    )
    assert "paste" in submitted or "link" in submitted, (
        "the document handed to the drafter never says a pasted full link is accepted, so the "
        "hint a person reads will not say it either"
    )


async def test_the_drafting_run_spends_the_callers_own_model() -> None:
    """ADR-008: the engine never holds a key, and the drafting call is not an exception to that."""
    svc, runs, repo = _service()
    run = _source(repo)

    await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    assert "c1" in json.dumps(runs.created[-1], ensure_ascii=False), (
        "the caller's own binding did not reach the run"
    )


# ── the answer ───────────────────────────────────────────────────────────────


async def test_a_well_formed_answer_becomes_the_proposed_fields() -> None:
    svc, _team_runs, repo = _service()
    run = _source(repo)

    drafted = await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    assert [f.name for f in drafted.fields] == ["Competitor", "Focus"]


async def test_a_model_answering_in_prose_is_a_curated_refusal_not_a_500() -> None:
    """Asking a model for JSON and getting a paragraph is an expected outcome of asking a model.
    The person is told the draft failed and can write the form themselves; they are not shown a
    server fault."""
    svc, _team_runs, repo = _service(_FakeTeamRuns(drafter_output="Sure! Here are some ideas:"))
    run = _source(repo)

    with pytest.raises(_error()) as caught:
        await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    assert getattr(caught.value, "status_code", None) == 422


# ── the collect path ─────────────────────────────────────────────────────────


async def test_a_drafter_slower_than_the_budget_hands_back_a_token() -> None:
    """Slow is not failed. The call returns something the console can re-present, the same way the
    intake read-back does, rather than holding an HTTP request past the gateway's read timeout."""
    svc, _team_runs, repo = _service(_FakeTeamRuns(settles=False))
    run = _source(repo)

    outcome = await svc.suggest(_principal(), team_run_id=run.id, models=_MODELS)

    assert isinstance(outcome, _pending())
    assert outcome.form_draft_run_id is not None


async def test_a_token_is_only_redeemable_against_a_drafting_run() -> None:
    """Fail-closed on run identity, on the first read, before any budget burns. Any other run of
    the same organisation would otherwise buy a poll and have its output read as a proposed form."""
    svc, team_runs, repo = _service()
    stranger = team_runs.seed(
        _RunRow("SUCCEEDED", manifest={"metadata": {"name": "competitor-brief"}}, results={})
    )

    with pytest.raises(_error()) as caught:
        await svc.suggest(_principal(), form_draft_run_id=stranger.id)

    assert getattr(caught.value, "status_code", None) == 422


async def test_collecting_a_token_that_names_no_run_is_a_404() -> None:
    """A token for a run that does not exist, or belongs to another organisation, is something the
    caller can act on — it must not escape the service and become a 500."""
    svc, _team_runs, _repo = _service()

    with pytest.raises(_error()) as caught:
        await svc.suggest(_principal(), form_draft_run_id=uuid.uuid4())

    assert getattr(caught.value, "status_code", None) == 404
