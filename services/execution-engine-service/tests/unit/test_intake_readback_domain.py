"""#866 — the read-back's pure rules: the length floor and the answer's shape.

Two things must be decidable without a model and without I/O.

**The floor.** "Too vague" is a deterministic character count checked BEFORE the model is called,
never the model's own judgement (ruled on #866). A model-judged refusal is not reproducible: the
same idea passes on Monday and is refused on Tuesday, and the founder has no way to understand
why. The floor is 80 characters of actual idea, so padding with whitespace does not buy a pass.

**The answer's shape.** A model returns whatever it likes. The endpoint's contract is narrow —
ordered spans each marked ``read`` or ``inferred``, at most three questions, a ``choice`` question
carrying its options and a ``text`` question carrying none — and it is this module, not the model,
that holds the contract. The cap is the endpoint's, not the caller's.

Seam imported FUNCTION-LOCALLY (``.claude/rules/tests-seam-imports.md``) — RED until the impl lands.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _floor():  # noqa: ANN202 — the seam ships its own signature
    from oraclous_execution_engine_service.domain.intake_readback import idea_meets_floor

    return idea_meets_floor


def _parse():  # noqa: ANN202
    from oraclous_execution_engine_service.domain.intake_readback import parse_readback

    return parse_readback


def _shape_error() -> type[Exception]:
    from oraclous_execution_engine_service.domain.intake_readback import ReadbackShapeError

    return ReadbackShapeError


def _min_chars() -> int:
    from oraclous_execution_engine_service.domain.intake_readback import IDEA_MIN_CHARS

    return IDEA_MIN_CHARS


def _max_questions() -> int:
    from oraclous_execution_engine_service.domain.intake_readback import MAX_QUESTIONS

    return MAX_QUESTIONS


# ── the floor ────────────────────────────────────────────────────────────────


def test_the_floor_is_eighty_characters() -> None:
    # Pinned as a number because the frontend tells the founder how much more to write.
    assert _min_chars() == 80


def test_one_character_under_the_floor_refuses() -> None:
    assert _floor()("x" * 79) is False


def test_exactly_the_floor_passes() -> None:
    assert _floor()("x" * 80) is True


def test_one_character_over_the_floor_passes() -> None:
    assert _floor()("x" * 81) is True


def test_surrounding_whitespace_does_not_count_toward_the_floor() -> None:
    # Otherwise the founder pads with spaces and gets a confident restatement of nothing.
    assert _floor()("   " + "x" * 78 + "   ") is False


def test_whitespace_only_and_empty_refuse() -> None:
    assert _floor()("") is False
    assert _floor()("          ") is False
    assert _floor()("\n\t " * 40) is False


def test_a_long_idea_of_real_prose_passes() -> None:
    idea = (
        "I want to build an ordering tool for independent bakeries that still take "
        "their weekend orders on paper and lose half of them."
    )
    assert len(idea) > 80
    assert _floor()(idea) is True


# ── the answer's shape ───────────────────────────────────────────────────────


def _good_payload(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "restatement": [
            {"text": "a tool for indie bakers ", "source": "read"},
            {"text": "who currently track orders on paper", "source": "inferred"},
        ],
        "questions": [
            {"id": "q1", "text": "How do they take orders today?", "kind": "text", "options": []},
            {
                "id": "q2",
                "text": "Who pays?",
                "kind": "choice",
                "options": ["the bakery", "the customer"],
            },
        ],
    }
    payload.update(over)
    return payload


def test_a_well_formed_answer_keeps_its_span_order() -> None:
    # Order is load-bearing: the screen joins the spans into one paragraph, so a reordering
    # turns a correct restatement into nonsense.
    spans, questions = _parse()(_good_payload())
    assert [s.text for s in spans] == [
        "a tool for indie bakers ",
        "who currently track orders on paper",
    ]
    assert [s.source for s in spans] == ["read", "inferred"]
    assert len(questions) == 2


def test_joining_the_spans_reproduces_the_restatement() -> None:
    spans, _ = _parse()(_good_payload())
    assert "".join(s.text for s in spans) == (
        "a tool for indie bakers who currently track orders on paper"
    )


def test_a_source_outside_read_and_inferred_is_rejected() -> None:
    # The screen renders inferred spans differently so the founder can correct them. A third
    # value has no rendering, and guessing one would print an inference as if it were read.
    payload = _good_payload(restatement=[{"text": "x", "source": "guessed"}])
    with pytest.raises(_shape_error()):
        _parse()(payload)


def test_a_span_missing_its_source_is_rejected() -> None:
    with pytest.raises(_shape_error()):
        _parse()(_good_payload(restatement=[{"text": "x"}]))


def test_an_empty_span_text_is_rejected() -> None:
    with pytest.raises(_shape_error()):
        _parse()(_good_payload(restatement=[{"text": "", "source": "read"}]))


def test_an_empty_restatement_is_rejected() -> None:
    # Above the floor the endpoint always restates. No spans means the model declined, and a
    # silent empty paragraph is worse than a refusal.
    with pytest.raises(_shape_error()):
        _parse()(_good_payload(restatement=[]))


def test_a_restatement_that_is_one_blob_of_prose_is_rejected() -> None:
    with pytest.raises(_shape_error()):
        _parse()(_good_payload(restatement="a tool for indie bakers"))


# ── the question cap and kinds ───────────────────────────────────────────────


def test_the_cap_is_three() -> None:
    assert _max_questions() == 3


def test_more_than_three_questions_are_cut_to_three() -> None:
    # Capped by the endpoint, not by the caller: a chatty model must not be able to put a fourth
    # question on the screen.
    many = [
        {"id": f"q{i}", "text": f"question {i}", "kind": "text", "options": []} for i in range(7)
    ]
    _, questions = _parse()(_good_payload(questions=many))
    assert len(questions) == 3
    assert [q.id for q in questions] == ["q0", "q1", "q2"]


def test_no_questions_at_all_is_allowed() -> None:
    # 0 to 3. An idea that leaves nothing worth asking is a fine outcome, not an error.
    _, questions = _parse()(_good_payload(questions=[]))
    assert questions == []


def test_a_choice_question_without_options_is_rejected() -> None:
    # The screen renders a choice as buttons. No options means no buttons and a dead question.
    payload = _good_payload(
        questions=[{"id": "q1", "text": "Who pays?", "kind": "choice", "options": []}]
    )
    with pytest.raises(_shape_error()):
        _parse()(payload)


def test_a_text_question_carrying_options_is_rejected() -> None:
    payload = _good_payload(
        questions=[{"id": "q1", "text": "Why?", "kind": "text", "options": ["a", "b"]}]
    )
    with pytest.raises(_shape_error()):
        _parse()(payload)


def test_an_unknown_question_kind_is_rejected() -> None:
    payload = _good_payload(
        questions=[{"id": "q1", "text": "Why?", "kind": "slider", "options": []}]
    )
    with pytest.raises(_shape_error()):
        _parse()(payload)


def test_a_question_with_no_text_is_rejected() -> None:
    payload = _good_payload(questions=[{"id": "q1", "text": "  ", "kind": "text", "options": []}])
    with pytest.raises(_shape_error()):
        _parse()(payload)


def test_duplicate_question_ids_are_rejected() -> None:
    # The answers come back keyed by id (`inputs.answers`, #849). Two questions sharing an id
    # means one founder answer silently overwrites the other.
    payload = _good_payload(
        questions=[
            {"id": "q1", "text": "a", "kind": "text", "options": []},
            {"id": "q1", "text": "b", "kind": "text", "options": []},
        ]
    )
    with pytest.raises(_shape_error()):
        _parse()(payload)


def test_a_missing_questions_key_is_rejected() -> None:
    payload = _good_payload()
    del payload["questions"]
    with pytest.raises(_shape_error()):
        _parse()(payload)


def test_a_non_dict_payload_is_rejected() -> None:
    for bad in ("just prose", ["a", "list"], None, 42):
        with pytest.raises(_shape_error()):
            _parse()(bad)
