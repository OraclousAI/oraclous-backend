"""#1137 / #1142 — the document the platform writes (domain layer).

#1137 shipped writing exactly the manifest's declared keys. #1142's root cause: that is too little.
Run `925dce0e-c7b0-4bfd-a7ba-5b743d06a1ef` on graph `ad5bb59c-b24e-4fd8-ab80-013dc4838fff` settled a
member whose answer parsed to nine keys (`posture`, `headline`, `rationale`, `sections`,
`hypotheses`, `kill_conditions`, `next_action`, `economics`, `artifact_refs`) — the platform saved a
172-character document carrying only the two the manifest declared required. The brief page renders
straight off this content, so the seven undeclared keys never existed as far as a reader was
concerned.

Amended ruling (#1142): the content is the member's WHOLE final answer object when one parses —
`payload["output"]` (the model's raw reply: prose, a fenced block, or a bare JSON object) is peeled
for the first embedded JSON object that carries every declared key, exactly the way
`team_run.py::_parse_member_object` peels a producer's hand-off (so a trailing `driving_signals`
receipt object, mandated for a tool-declaring member, is never the one that wins). Every key that
object carries survives into the content, with the declared keys — sourced from `payload`, already
guaranteed present by `should_autosave` — always present too. The surrounding envelope (`status`,
`simulated`, `fetched_urls`, `unverified_links`, `output`, `steps`, `driving_signals`, and any
harness `error_type`/`error_message`) is never part of the content: none of it is the member's own
answer.

A member whose final answer does not parse to a JSON object — no `output` at all, prose with no
embedded object, or an embedded value that decodes to something other than an object — still saves
exactly its declared keys. That is #1137's original behaviour, kept as the floor: nothing regresses
to writing nothing.

Two of the three historical save failures are Neo4j/KGS ingestion mechanics, not model behaviour,
and both are avoided by construction here, unchanged by #1142:

  * run `28fcb3f6`, jobs `1ebb465e`/`5362dcbd`: ``Neo.ClientError.Statement.TypeError`` — a
    structured ``source_type`` routed the brief's nested ``hypotheses[]`` objects onto Neo4j node
    properties, which only accept primitives/arrays thereof. The platform write always uses
    ``source_type="text"``; nested values are serialised INSIDE the content string, never handed
    on as structure.
  * run `a50dc40d`, job `c1e5d1b5`: an un-parseable, truncated JSON tool argument. The platform
    never re-emits a model-authored argument — the content is built from the already-validated,
    already-stored settle payload.

No ``title`` (``derive_name`` falls back to the member role). The producer stamp is passed through
verbatim: this module does not mint identity, it only carries the caller's stamp (the same one
``team_run.py::_producer_ref`` mints for the tool path) onto the document unchanged. Unaffected by
#1142.

PINS CHANGED from #1137's tests PR (#1138), and why: ``test_content_is_the_canonical_json_of_
exactly_the_declared_keys`` asserted the content is EXACTLY the declared keys and nothing else —
that pin was the defect (#1142's root cause), so it is replaced by
``test_content_carries_the_members_whole_final_answer_when_one_parses`` plus the explicit floor and
envelope-exclusion tests below. Every other test in this file (plain-string content, always
``text``, no title, producer passthrough, a nested value surviving) is a behaviour #1142 does not
touch and is kept as-is; ``test_a_nested_value_survives_intact_inside_the_content_string`` now reads
as the FLOOR case (no ``output`` on the payload) rather than the general case.

RED until ``domain/member_artifact.py``'s ``build_document`` peels ``payload["output"]``; the seam
is imported function-locally per ``.claude/rules/tests-seam-imports.md``.
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.unit]

# The same six fields _producer_ref (team_run.py) mints for the tool path.
_PRODUCER = {
    "producer_kind": "team-member",
    "member_role": "synthesizer",
    "attempt_id": "11111111-1111-1111-1111-111111111111",
    "team_run_id": "6332197e-0b1b-4845-bd9e-230a27bbaf38",
    "team_id": "22222222-2222-2222-2222-222222222222",
    "ordinal": 0,
}

# The nine-key shape from the #1142 bug report (run 925dce0e), trimmed to what the assertions need.
_FULL_ANSWER = {
    "posture": "prerequisite",
    "headline": "needs clearer marketing strategy",
    "rationale": "the plan assumes a channel that has not been validated",
    "sections": [
        {
            "title": "go-to-market",
            "claims": ["channel X converts at 4%"],
            "evidence": ["survey n=40"],
            "sources": ["doc:survey-1"],
        }
    ],
    "hypotheses": [{"text": "price is the blocker", "label": "hypothesis", "experiment": "survey"}],
    "kill_conditions": ["conversion stays below 1% after two channels"],
    "next_action": "run the channel-X survey before committing spend",
    "economics": {"cac_estimate_usd": 42, "payback_months": 9},
    "artifact_refs": ["doc:survey-1"],
}


def _envelope(**over: object) -> dict:
    """The dispatch envelope ``team_run.py`` builds per member, with #1137's lift of declared
    keys onto the top level already applied — the shape ``_autosave_member_artifact`` actually
    hands to ``build_document`` in production."""
    base = {
        "output": json.dumps(_FULL_ANSWER),
        "status": "succeeded",
        "steps": [{"index": 0, "kind": "tool"}],
        "driving_signals": [],
        "simulated": False,
        "unverified_links": [],
        "fetched_urls": [],
        # #1137's own lift: only the DECLARED keys land on the envelope's top level.
        "posture": _FULL_ANSWER["posture"],
        "headline": _FULL_ANSWER["headline"],
    }
    base.update(over)
    return base


def test_content_carries_the_members_whole_final_answer_when_one_parses() -> None:
    """The #1142 fix: every key the member produced — not only the two the manifest declared —
    survives into the saved content."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(), declared_keys=["posture", "headline"], producer=_PRODUCER
    )

    assert json.loads(doc["content"]) == _FULL_ANSWER


def test_the_declared_keys_are_still_present_alongside_the_rest() -> None:
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(), declared_keys=["posture", "headline"], producer=_PRODUCER
    )

    content = json.loads(doc["content"])
    assert content["posture"] == _FULL_ANSWER["posture"]
    assert content["headline"] == _FULL_ANSWER["headline"]


def test_the_surrounding_envelope_is_never_saved_into_content() -> None:
    """Status, the simulated flag, and the fetched/unverified link lists are the run's bookkeeping,
    not the member's answer — none of it may leak into the graph."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(
            simulated=True,
            unverified_links=["https://unverified.example"],
            fetched_urls=["https://fetched.example"],
        ),
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    content = json.loads(doc["content"])
    for envelope_key in ("status", "simulated", "unverified_links", "fetched_urls", "output"):
        assert envelope_key not in content, (envelope_key, content)


def test_a_trailing_driving_signals_receipt_object_does_not_leak_into_content() -> None:
    """A tool-declaring member's reply carries the answer object followed by a SEPARATE
    ``driving_signals`` receipt object (#1043's ``REVIEWER_PROMPT`` shape). The peel must pick the
    object that carries the declared keys, never the receipt that happens to follow it."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    receipt = {"driving_signals": [{"signal": "ran", "value": True, "source_tool_call_id": "c1"}]}
    output = json.dumps(_FULL_ANSWER) + "\n\n" + json.dumps(receipt)
    doc = build_document(
        payload=_envelope(output=output),
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    content = json.loads(doc["content"])
    assert content == _FULL_ANSWER
    assert "driving_signals" not in content


def test_a_final_answer_wrapped_in_prose_is_still_parsed() -> None:
    """A real model rarely answers with bare JSON — the peel must look past narration."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    output = "Here is my assessment:\n\n" + json.dumps(_FULL_ANSWER) + "\n\nLet me know if useful."
    doc = build_document(
        payload=_envelope(output=output),
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    assert json.loads(doc["content"]) == _FULL_ANSWER


def test_a_final_answer_that_is_not_a_parseable_object_still_saves_the_declared_keys() -> None:
    """The floor: #1137's original behaviour survives unchanged for a member whose answer never
    parses to an object at all. Nothing regresses to writing nothing."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(output="I judged the launch not ready, no structured answer follows."),
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    assert json.loads(doc["content"]) == {
        "posture": _FULL_ANSWER["posture"],
        "headline": _FULL_ANSWER["headline"],
    }


def test_a_final_answer_that_parses_to_a_json_array_still_saves_the_declared_keys() -> None:
    """Valid JSON that is not an OBJECT (e.g. a bare array) is not a final-answer object either —
    the floor applies the same way as unparseable prose."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(output=json.dumps(["ready", "needs clearer marketing strategy"])),
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    assert json.loads(doc["content"]) == {
        "posture": _FULL_ANSWER["posture"],
        "headline": _FULL_ANSWER["headline"],
    }


def test_a_missing_output_key_on_the_payload_still_saves_the_declared_keys() -> None:
    """A payload built before #975/#1137 (or a synthetic caller) with no ``output`` key at all is
    the same floor case, not a crash."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    payload = {"posture": "prerequisite", "headline": "x"}
    doc = build_document(payload=payload, declared_keys=["posture", "headline"], producer=_PRODUCER)

    assert json.loads(doc["content"]) == {"posture": "prerequisite", "headline": "x"}


def test_a_nested_structure_inside_the_whole_answer_survives_intact() -> None:
    """A nested object (``sections``, three levels deep) among the UNDECLARED keys round-trips
    exactly, just carried as text rather than structure — the Neo4j map-property regression this
    module exists to avoid."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(), declared_keys=["posture", "headline"], producer=_PRODUCER
    )

    content = json.loads(doc["content"])
    assert content["sections"] == _FULL_ANSWER["sections"]
    assert content["hypotheses"] == _FULL_ANSWER["hypotheses"]
    assert content["economics"] == _FULL_ANSWER["economics"]


def test_a_nested_value_survives_intact_inside_the_content_string() -> None:
    """The floor case: a nested object carried directly as a DECLARED key's own value (no
    ``output`` on the payload) is not dropped or flattened either."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    hypotheses = [{"text": "price is the blocker", "label": "hypothesis", "experiment": "survey"}]
    payload = {"headline": "x", "hypotheses": hypotheses}
    doc = build_document(
        payload=payload, declared_keys=["headline", "hypotheses"], producer=_PRODUCER
    )

    assert json.loads(doc["content"])["hypotheses"] == hypotheses


def test_content_is_a_plain_string_never_structure() -> None:
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(), declared_keys=["posture", "headline"], producer=_PRODUCER
    )

    assert isinstance(doc["content"], str)


def test_source_type_is_always_text_even_with_nested_values() -> None:
    """The Neo4j map-property TypeError regression: a nested object among the answer's keys must
    never push this off the ``text`` path."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(), declared_keys=["posture", "headline"], producer=_PRODUCER
    )

    assert doc["source_type"] == "text"


def test_no_title_is_set() -> None:
    """``derive_name`` (KGS) falls back to the member role when no title is given — the platform
    write deliberately supplies none."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(), declared_keys=["posture", "headline"], producer=_PRODUCER
    )

    assert doc.get("title") is None


def test_the_producer_stamp_is_carried_through_verbatim() -> None:
    """The document mints no identity of its own — whatever six-field stamp the caller built (the
    same shape ``_producer_ref`` mints for the tool path) rides onto the document unchanged."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload=_envelope(), declared_keys=["posture", "headline"], producer=_PRODUCER
    )

    assert doc["producer"] == _PRODUCER
