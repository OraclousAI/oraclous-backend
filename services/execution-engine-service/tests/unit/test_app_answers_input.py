"""#846 — a validation-desk intake answer reaches every member, and an "I don't know" one arrives
marked as an assumption to test rather than a premise to rely on.

ADR-052 decision 3 (``oraclous-knowledge``, app-descriptor layer for generated apps) names the
shape: the app ships ``answers[].hypothesis`` as a ONE-OFF field private to that app, temporary
until the descriptor layer lands, with **no change to ``validate_input_keys``'s general contract**.
So ``inputs["answers"]`` becomes a fourth consumable key beside the declared ``task_input.key``, a
member's ``fan_out.over`` (#599) and the engine-reserved ``_refresh_seed`` (#602).

Two halves, and only both together are worth anything:

- **Accepted.** ``validate_input_keys`` fail-closes any undeclared ``inputs`` key (#714 defect (b)),
  so ``answers`` is a 422 today and the app's approve-starts-the-run step cannot fire.
- **Read.** A key that passes the gate but that nothing renders is the SILENT DISCARD #714 closed —
  the caller's input vanishes and the model fills the hole with fiction (run ``538ab1fa``). So the
  answers must reach every member's harness input, the hypothesis-flagged ones under a directive
  that forbids treating them as given.

Frontend ``OraclousAI/oraclous-frontend#210`` criterion 3 is the driver: "I don't know" is a
first-class intake answer and "is submitted in a form the run records as a hypothesis, not a fact".

RED-by-design until the ``[impl]`` lands: ``parse_answers``/``validate_answers``/
``resolve_run_answers`` and ``render_member_input``'s ``answers`` keyword do not exist yet, so every
seam is imported function-locally (§4.1) and these fail at runtime, never at collection.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
    OHMTermination,
)
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_USER = uuid.uuid4()

# The intake shape ADR-052 pins: one confirmed answer and two the founder did not know.
_Q_SEGMENT = "Who is the target customer?"
_Q_PRICE = "What will they pay?"
_Q_STAGE = "What stage are you at?"
_A_PRICE = "maybe $50/mo, unsure"
_A_STAGE = "pre-seed, two founders"

_HYPOTHESIS_ITEMS: list[dict[str, Any]] = [
    {"question": _Q_SEGMENT, "answer": None, "hypothesis": True},
    {"question": _Q_PRICE, "answer": _A_PRICE, "hypothesis": True},
]
_CONFIRMED_ITEM: dict[str, Any] = {"question": _Q_STAGE, "answer": _A_STAGE, "hypothesis": False}
_ANSWERS = [*_HYPOTHESIS_ITEMS, _CONFIRMED_ITEM]


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _team_document(
    *,
    task_input: dict[str, Any] | None = None,
    fan_out_over: str | None = None,
) -> dict[str, Any]:
    member: dict[str, Any] = {
        "role": "researcher",
        "kind": "agent",
        "manifest_ref": "org:x/researcher@1",
        "subgoal": "research the demand signal",
    }
    if fan_out_over is not None:
        member["fan_out"] = {"over": fan_out_over, "max_parallel": 2}
    doc: dict[str, Any] = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "validation-desk",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [member],
        "runtime": {"entrypoint": "researcher"},
    }
    if task_input is not None:
        doc["task_input"] = task_input
    return doc


def _two_member_document() -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "validation-desk",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/researcher@1",
                "subgoal": "research",
            },
            {
                "role": "synthesizer",
                "kind": "agent",
                "manifest_ref": "org:x/synthesizer@1",
                "subgoal": "synthesize",
                "depends_on": ["researcher"],
            },
        ],
        "runtime": {"entrypoint": "researcher"},
    }


def _member(role: str = "researcher") -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", subgoal=f"{role} work"
    )


class _FakeHarness:
    """Records every dispatch's rendered input and always succeeds."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def execute(
        self,
        *,
        input_text: str,
        manifest_ref: str | None = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        self.inputs.append(input_text)
        return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ran", "steps": []}


def _render(**kw: Any) -> str:
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        render_member_input,
    )

    return render_member_input(_member(), [], **kw)


def _parsed(items: list[dict[str, Any]]) -> Any:
    """``parse_answers``'s return value for a raw item list — the shape ``render_member_input``
    takes. Going through the parser (rather than hand-building it) keeps the two seams honest
    about each other."""
    from oraclous_execution_engine_service.domain.app_answers import (  # §4.1 seam
        parse_answers,
    )

    return parse_answers({"answers": items})


# ── half 1: the key is accepted, and nothing else about the gate moves ───────────────────────


def test_the_answers_key_passes_the_undeclared_key_gate() -> None:
    """The reported block: a team declaring no ``task_input`` and no ``fan_out`` still has to accept
    the app's own ``answers`` field, or approve-starts-the-run is a 422."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    validate_input_keys(load_ohm(_team_document()), {"answers": _ANSWERS})


def test_another_undeclared_key_is_still_a_422() -> None:
    """Acceptance criterion 2: ``validate_input_keys`` is unchanged for every key that is not this
    one. ``answers`` riding alongside does not launder its neighbour through the gate."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_input_keys,
    )

    with pytest.raises(TeamRunError) as err:
        validate_input_keys(
            load_ohm(_team_document()),
            {"answers": _ANSWERS, "pr_url": "https://example.invalid/1"},
        )
    assert err.value.status_code == 422
    assert err.value.error_type == "undeclared_input_key"
    assert "pr_url" in str(err.value)


def test_the_422_message_does_not_advertise_the_one_off_key() -> None:
    """``answers`` is temporary debt (ADR-052 decision 3), not a feature to publicise. The
    "it consumes …" list stays the team's OWN declared keys, exactly as ``_refresh_seed`` is
    already hidden — otherwise every 422 teaches callers to reach for a field that is scheduled
    for removal."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_input_keys,
    )

    manifest = load_ohm(_team_document(task_input={"required": False, "key": "idea"}))
    with pytest.raises(TeamRunError) as err:
        validate_input_keys(manifest, {"pr_url": "https://example.invalid/1"})
    message = str(err.value)
    assert "idea" in message  # it still says what the team DOES consume
    assert "answers" not in message
    assert "_refresh_seed" not in message


@pytest.mark.parametrize(
    "bad",
    [
        {"answers": "who is the customer?"},  # not a list
        {"answers": {"question": _Q_PRICE}},  # a dict, not a list of them
        {"answers": ["who is the customer?"]},  # an item that is not an object
        {"answers": [{"answer": _A_PRICE}]},  # no question
        {"answers": [{"question": "", "answer": _A_PRICE}]},  # empty question
        {"answers": [{"question": "   ", "answer": _A_PRICE}]},  # whitespace-only question
        {"answers": [{"question": 42, "answer": _A_PRICE}]},  # non-string question
        {"answers": [{"question": _Q_PRICE, "answer": 50}]},  # non-string, non-null answer
        {"answers": [{"question": _Q_PRICE, "answer": _A_PRICE, "hypothesis": "yes"}]},  # not bool
    ],
)
def test_a_malformed_answers_payload_is_a_422(bad: dict[str, Any]) -> None:
    """Fail-closed (CLAUDE.md §3.5): a shape the renderer cannot read is rejected at create, never
    half-rendered and never silently dropped."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_answers,
    )

    with pytest.raises(TeamRunError) as err:
        validate_answers(bad)
    assert err.value.status_code == 422
    assert err.value.error_type == "invalid_answers"


def test_the_malformed_message_never_echoes_the_founder_s_words() -> None:
    """CLAUDE.md §11: a customer's prompt text is never reproduced in an error message. The 422
    describes the expected SHAPE and nothing the founder typed."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_answers,
    )

    with pytest.raises(TeamRunError) as err:
        validate_answers({"answers": [{"question": _Q_PRICE, "answer": 50}]})
    message = str(err.value)
    assert _Q_PRICE not in message
    assert _A_PRICE not in message


@pytest.mark.parametrize("empty", [None, {}, {"answers": []}])
def test_absent_or_empty_answers_pass(empty: dict[str, Any] | None) -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_answers,
    )

    validate_answers(empty)


class _RefusingRepo:
    """Any write is a failure — the 422 has to land before the run is persisted or enqueued."""

    async def create(self, **_kw: Any) -> Any:
        raise AssertionError("create persisted a run with a malformed answers payload")


async def test_create_rejects_a_malformed_payload_before_persisting_or_enqueueing() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (
        TeamRunError,
        TeamRunService,
    )

    enqueued: list[uuid.UUID] = []
    svc = TeamRunService(
        team_runs=_RefusingRepo(),  # type: ignore[arg-type] — duck-typed seam in unit tests
        harness=None,
        enqueue=lambda rid, _org, _user: enqueued.append(rid),
    )
    with pytest.raises(TeamRunError) as err:
        await svc.create(
            _principal(),
            manifest=_team_document(),
            sub_harnesses={},
            gate_decisions={},
            inputs={"answers": [{"question": _Q_PRICE, "answer": 50}]},
        )
    assert err.value.status_code == 422
    assert err.value.error_type == "invalid_answers"
    assert enqueued == []  # no worker, no tokens


# ── half 2: the answers actually reach a member ──────────────────────────────────────────────


def test_a_hypothesis_flagged_answer_renders_under_a_directive_forbidding_a_premise() -> None:
    """The whole point of the field. The member is told, in the same breath as the question, that
    this is an assumption to test — otherwise it researches a fabricated fact for half an hour and
    produces an impeccably-cited wrong brief."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        HYPOTHESIS_DIRECTIVE,
    )

    rendered = _render(answers=_parsed(_HYPOTHESIS_ITEMS))
    assert HYPOTHESIS_DIRECTIVE in rendered
    assert _Q_SEGMENT in rendered
    assert _Q_PRICE in rendered
    assert _A_PRICE in rendered


def test_an_unanswered_question_renders_as_unanswered_not_as_an_empty_string() -> None:
    """``answer: null`` is "I don't know", which is not the same as an answer of "". A blank reads
    to a model as an omission it may fill in; the field has to say the founder did not know."""
    rendered = _render(answers=_parsed([{"question": _Q_SEGMENT, "answer": None}]))
    line = next(ln for ln in rendered.splitlines() if _Q_SEGMENT in ln)
    assert not line.rstrip().endswith("—")  # never a dangling separator with nothing after it
    assert "no answer given" in line


def test_a_confirmed_answer_renders_outside_the_unverified_block() -> None:
    """An answer the founder DID give is a given, and it must reach the member too — dropping it
    would be the same silent discard #714 closed. It just must not sit under the directive that
    tells the member to distrust it."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        HYPOTHESIS_DIRECTIVE,
    )

    rendered = _render(answers=_parsed([_CONFIRMED_ITEM]))
    assert _Q_STAGE in rendered
    assert _A_STAGE in rendered
    assert HYPOTHESIS_DIRECTIVE not in rendered  # nothing is flagged, so no directive at all


def test_a_missing_hypothesis_flag_means_confirmed() -> None:
    """``hypothesis`` defaults to false, so an ordinary answer needs no flag and an app that never
    flags anything sends a plain list."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        HYPOTHESIS_DIRECTIVE,
    )

    rendered = _render(answers=_parsed([{"question": _Q_STAGE, "answer": _A_STAGE}]))
    assert _A_STAGE in rendered
    assert HYPOTHESIS_DIRECTIVE not in rendered


def test_a_mixed_list_separates_the_two_groups() -> None:
    """The confirmed answer must not drift under the unverified directive, and vice versa — that
    inversion is worse than not sending either."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        HYPOTHESIS_DIRECTIVE,
    )

    rendered = _render(answers=_parsed(_ANSWERS))
    assert HYPOTHESIS_DIRECTIVE in rendered
    for text in (_Q_SEGMENT, _Q_PRICE, _A_PRICE, _Q_STAGE, _A_STAGE):
        assert text in rendered
    directive_at = rendered.index(HYPOTHESIS_DIRECTIVE)
    assert rendered.index(_Q_STAGE) < directive_at  # the confirmed answer sits ABOVE the directive
    assert rendered.index(_Q_SEGMENT) > directive_at  # both flagged ones sit BELOW it
    assert rendered.index(_Q_PRICE) > directive_at


def test_no_answers_renders_byte_identical_to_today() -> None:
    """Default-OFF, the #602/#674 discipline: a run without the field renders exactly the input it
    renders today — no empty block, no reordering."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        render_member_input,
    )

    assert _render(answers=None) == render_member_input(_member(), [])


def test_the_answers_sit_after_the_task_and_before_the_execution_directive() -> None:
    """Ordering is the whole framing: the member reads the job, then what is known, then what is
    only assumed, and only then how to execute."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        EXECUTION_DIRECTIVE,
        HYPOTHESIS_DIRECTIVE,
    )

    task = "Validate whether this idea is worth pursuing."
    rendered = _render(task=task, answers=_parsed(_ANSWERS))
    assert rendered.index(f"Task: {task}") < rendered.index(HYPOTHESIS_DIRECTIVE)
    assert rendered.index(HYPOTHESIS_DIRECTIVE) < rendered.index(EXECUTION_DIRECTIVE)


# ── half 2, wired: every member on every dispatch path ───────────────────────────────────────


async def test_every_member_receives_the_answers() -> None:
    """Like the task (#674): the synthesizer must not have to reconstruct what was assumed from the
    researcher's hand-off."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        run_team_harness,
    )

    harness = _FakeHarness()
    await run_team_harness(load_ohm(_two_member_document()), harness, inputs={"answers": _ANSWERS})
    assert len(harness.inputs) == 2
    for rendered in harness.inputs:
        assert _Q_SEGMENT in rendered
        assert _A_STAGE in rendered


async def test_a_fan_out_member_receives_the_answers_on_every_item() -> None:
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        run_team_harness,
    )

    harness = _FakeHarness()
    await run_team_harness(
        load_ohm(_team_document(fan_out_over="$.items")),
        harness,
        inputs={"items": ["i1", "i2"], "answers": _ANSWERS},
    )
    assert len(harness.inputs) == 2  # one dispatch per fan item
    for rendered in harness.inputs:
        assert _Q_SEGMENT in rendered


async def test_a_loop_team_receives_the_answers_too() -> None:
    """The hybrid driver (ADR-043) builds its own dispatch. A team with a genuine loop must not be
    the one shape where the founder's assumptions silently vanish."""
    from oraclous_execution_engine_service.services.team_run import (  # §4.1 seam
        run_team_hybrid,
    )

    async def coordinate(loop: OHMLoop, results: dict[str, Any], rounds_left: int) -> list[str]:
        return [r for r in loop.members if results.get(r) is None]

    def done_check_for(loop: OHMLoop, diag: dict[str, Any] | None = None):
        async def done(results: dict[str, Any]) -> bool:
            return all(results.get(r) is not None for r in loop.members)

        return done

    members = [_member("researcher"), _member("critic")]
    manifest = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id=uuid.uuid4(), name="validation-desk", owner_organization_id=_ORG, kind="team"
        ),
        members=members,
        orchestration=OHMOrchestration(
            loops=[
                OHMLoop(
                    members=["researcher", "critic"],
                    routing={"researcher": "research", "critic": "critique"},
                )
            ],
            termination=OHMTermination(max_rounds=5),
        ),
        runtime=OHMRuntime(entrypoint="researcher"),
    )
    harness = _FakeHarness()
    await run_team_hybrid(
        manifest,
        harness,
        coordinate=coordinate,
        done_check_for=done_check_for,
        inputs={"answers": _ANSWERS},
    )
    assert harness.inputs
    for rendered in harness.inputs:
        assert _Q_SEGMENT in rendered
