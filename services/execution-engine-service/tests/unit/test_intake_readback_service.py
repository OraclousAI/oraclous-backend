"""#866 — the read-back service: refuse before the model, run one member, collect or hand back.

The desk's first step takes a paragraph of free text. Nothing on the platform read it before this
endpoint existed, so the three questions under it were hardcoded and there was no restatement at
all. This service is the thing that reads it.

Four behaviours are pinned here, and each one is a decision that could have gone another way:

- **The floor refuses without spending anything.** Under 80 characters, no model is called at all.
  The test asserts the submit seam was never touched, because "instant" is the point: a founder
  who typed one line gets told so immediately, not after a model round trip.
- **No model bound refuses too.** There is deliberately no platform fallback model on this path
  (ruled on #866). The call refuses rather than silently borrowing one, which is why the
  ``MODEL_NOT_CONNECTED`` code exists.
- **A slow model is not a failure.** The run is polled up to a budget that sits under the
  gateway's read timeout; past it the caller gets a run id back and re-calls to collect. This is
  the same shape ``refine-nl`` (#595) already ships.
- **A run id is only redeemable against a read-back run.** Handing in any other run of the same
  organisation is a curated refusal on the first read, not a burned budget and a confusing peel.

The model run is faked at the ``TeamRunService`` seam. The REAL model path is the deployed
end-to-end run's job — a faked model is never a proof of done.

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
_USER = uuid.uuid4()

_GOOD_IDEA = (
    "An ordering tool for independent bakeries that still take their weekend orders "
    "on paper and lose track of half of them."
)
_SHORT_IDEA = "a bakery app"

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
        "restatement": [
            {"text": "an ordering tool for independent bakeries ", "source": "read"},
            {"text": "whose owners lose paper orders", "source": "inferred"},
        ],
        "questions": [
            {"id": "q1", "text": "How many orders a week?", "kind": "text", "options": []}
        ],
    }
)


def _principal(org: uuid.UUID | None = _ORG) -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=org)


class _RunRow:
    def __init__(
        self,
        state: str,
        results: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.state = state
        self.results = results
        self.manifest = manifest or {"metadata": {"name": "intake-reader"}}


class _FakeTeamRuns:
    """The TeamRunService seam the read-back consumes: create (submit) + get (poll/read)."""

    reader_output: str = _ANSWER

    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, _RunRow] = {}
        self.created: list[dict[str, Any]] = []

    def seed(
        self,
        state: str,
        results: dict[str, Any] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> _RunRow:
        row = _RunRow(state, results, manifest)
        self.runs[row.id] = row
        return row

    async def create(self, principal: Principal, **kw: Any) -> _RunRow:
        self.created.append(kw)
        row = _RunRow(
            "SUCCEEDED", {"reader": {"output": self.reader_output, "status": "SUCCEEDED"}}
        )
        self.runs[row.id] = row
        return row

    async def get(self, run_id: uuid.UUID, principal: Principal) -> _RunRow:
        row = self.runs.get(run_id)
        if row is None:
            from oraclous_execution_engine_service.services.team_run_service import TeamRunError

            raise TeamRunError("team run not found", 404)
        return row


def _service(team_runs: _FakeTeamRuns | None = None):  # noqa: ANN202 — seam ships its own type
    from oraclous_execution_engine_service.services.intake_readback_service import (
        IntakeReadbackService,
    )

    team_runs = team_runs or _FakeTeamRuns()
    svc = IntakeReadbackService(
        team_runs=team_runs,  # type: ignore[arg-type] — duck-typed seam in unit tests
        readback_poll_seconds=0.2,
        readback_poll_interval_seconds=0.01,
    )
    return svc, team_runs


def _error() -> type[Exception]:
    from oraclous_execution_engine_service.services.intake_readback_service import (
        IntakeReadbackError,
    )

    return IntakeReadbackError


def _pending() -> type:
    from oraclous_execution_engine_service.services.intake_readback_service import PendingReadback

    return PendingReadback


def _readback() -> type:
    from oraclous_execution_engine_service.services.intake_readback_service import Readback

    return Readback


# ── refusals that never reach a model ────────────────────────────────────────


async def test_an_idea_under_the_floor_refuses_without_calling_a_model() -> None:
    svc, team_runs = _service()
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), idea=_SHORT_IDEA, models=_MODELS)
    assert exc.value.status_code == 422
    assert exc.value.error_code == "IDEA_TOO_VAGUE"
    # the whole point of a deterministic floor: nothing was spent and the answer was instant
    assert team_runs.created == []


async def test_the_floor_is_checked_before_the_missing_model_refusal() -> None:
    # Both are wrong; the founder should be told about the one they can fix by typing more, and
    # the check that costs nothing runs first.
    svc, team_runs = _service()
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), idea=_SHORT_IDEA, models=[])
    assert exc.value.error_code == "IDEA_TOO_VAGUE"
    assert team_runs.created == []


async def test_no_model_bound_refuses_and_never_borrows_one() -> None:
    svc, team_runs = _service()
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), idea=_GOOD_IDEA, models=[])
    assert exc.value.status_code == 409
    assert exc.value.error_code == "MODEL_NOT_CONNECTED"
    assert team_runs.created == []


async def test_models_omitted_entirely_refuses_the_same_way() -> None:
    svc, _ = _service()
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), idea=_GOOD_IDEA, models=None)
    assert exc.value.error_code == "MODEL_NOT_CONNECTED"


async def test_a_principal_with_no_organisation_is_refused() -> None:
    # Fail-closed tenancy (ADR-006): there is no code path that reads without an organisation.
    svc, _ = _service()
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(org=None), idea=_GOOD_IDEA, models=_MODELS)
    assert exc.value.status_code == 403


# ── the happy path ───────────────────────────────────────────────────────────


async def test_it_reads_the_idea_with_the_callers_own_model() -> None:
    svc, team_runs = _service()
    out = await svc.readback(_principal(), idea=_GOOD_IDEA, models=_MODELS)
    assert isinstance(out, _readback())
    submitted = team_runs.created[0]
    # the founder's own binding is on both the team document and the member — no platform model
    assert submitted["manifest"]["models"][0]["config"]["credential_id"] == "c1"
    assert submitted["sub_harnesses"]["reader"]["models"][0]["binding"] == "openrouter/x"
    # one member, and the idea is in front of it verbatim
    assert len(submitted["manifest"]["members"]) == 1
    assert _GOOD_IDEA in submitted["manifest"]["members"][0]["subgoal"]


async def test_the_answer_comes_back_as_ordered_spans_and_questions() -> None:
    svc, _ = _service()
    out = await svc.readback(_principal(), idea=_GOOD_IDEA, models=_MODELS)
    assert [s.source for s in out.restatement] == ["read", "inferred"]
    assert "".join(s.text for s in out.restatement).startswith("an ordering tool")
    assert len(out.questions) == 1
    assert out.readback_run_id is not None


async def test_a_chatty_model_is_still_capped_at_three_questions() -> None:
    # The cap belongs to the endpoint. A model that asks seven does not get seven on the screen.
    team_runs = _FakeTeamRuns()
    team_runs.reader_output = json.dumps(
        {
            "restatement": [{"text": "a bakery ordering tool", "source": "read"}],
            "questions": [
                {"id": f"q{i}", "text": f"q {i}", "kind": "text", "options": []} for i in range(7)
            ],
        }
    )
    svc, _ = _service(team_runs)
    out = await svc.readback(_principal(), idea=_GOOD_IDEA, models=_MODELS)
    assert len(out.questions) == 3


async def test_a_model_that_answers_in_prose_is_a_curated_refusal_not_a_500() -> None:
    team_runs = _FakeTeamRuns()
    team_runs.reader_output = "Sure! Here is what I think you are building: a bakery app."
    svc, _ = _service(team_runs)
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), idea=_GOOD_IDEA, models=_MODELS)
    assert exc.value.status_code == 422


async def test_a_model_answer_in_the_wrong_shape_is_a_curated_refusal() -> None:
    team_runs = _FakeTeamRuns()
    team_runs.reader_output = json.dumps(
        {"restatement": [{"text": "x", "source": "guessed"}], "questions": []}
    )
    svc, _ = _service(team_runs)
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), idea=_GOOD_IDEA, models=_MODELS)
    assert exc.value.status_code == 422


# ── the slow model ───────────────────────────────────────────────────────────


async def test_a_model_slower_than_the_budget_hands_back_a_run_id() -> None:
    svc, team_runs = _service()
    still_running = team_runs.seed("RUNNING")
    out = await svc.readback(_principal(), readback_run_id=still_running.id)
    assert isinstance(out, _pending())
    assert out.readback_run_id == still_running.id


async def test_a_settled_run_is_collected_by_id() -> None:
    svc, team_runs = _service()
    settled = team_runs.seed("SUCCEEDED", {"reader": {"output": _ANSWER}})
    out = await svc.readback(_principal(), readback_run_id=settled.id)
    assert isinstance(out, _readback())
    assert out.readback_run_id == settled.id


async def test_a_failed_run_is_a_curated_refusal() -> None:
    svc, team_runs = _service()
    failed = team_runs.seed("FAILED")
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), readback_run_id=failed.id)
    assert exc.value.status_code == 422


async def test_a_run_id_that_is_not_a_read_back_run_is_refused_on_the_first_read() -> None:
    # Fail-closed on identity: a compiler run id must not buy a 25-second poll and a peel that
    # reads someone else's output as a restatement.
    svc, team_runs = _service()
    imposter = team_runs.seed("RUNNING", manifest={"metadata": {"name": "harness-compiler"}})
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal(), readback_run_id=imposter.id)
    assert exc.value.status_code == 422
    assert exc.value.error_type == "not_a_readback_run"


async def test_neither_an_idea_nor_a_run_id_is_a_refusal() -> None:
    svc, _ = _service()
    with pytest.raises(_error()) as exc:
        await svc.readback(_principal())
    assert exc.value.status_code == 422
