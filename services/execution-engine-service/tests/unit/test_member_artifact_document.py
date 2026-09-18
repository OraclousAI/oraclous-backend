"""#1137 — the document the platform writes (domain layer).

Two of the three historical save failures are Neo4j/KGS ingestion mechanics, not model behaviour,
and both are avoided by construction here:

  * run `28fcb3f6`, jobs `1ebb465e`/`5362dcbd`: ``Neo.ClientError.Statement.TypeError`` — a
    structured ``source_type`` routed the brief's nested ``hypotheses[]`` objects onto Neo4j node
    properties, which only accept primitives/arrays thereof. The platform write always uses
    ``source_type="text"``; nested values are serialised INSIDE the content string, never handed
    on as structure.
  * run `a50dc40d`, job `c1e5d1b5`: an un-parseable, truncated JSON tool argument. The platform
    never re-emits a model-authored argument — the content is built from the already-validated,
    already-stored settle payload.

Ruled: content is the canonical JSON of EXACTLY the declared keys — nothing merged in from the
rest of the payload (``output``, ``steps``, ``driving_signals``, or any undeclared key) — and no
``title`` (``derive_name`` falls back to the member role). The producer stamp is passed through
verbatim: this module does not mint identity, it only carries the caller's stamp (the same one
``team_run.py::_producer_ref`` mints for the tool path) onto the document unchanged.

RED until ``domain/member_artifact.py`` and ``build_document`` exist; the seam is imported
function-locally per ``.claude/rules/tests-seam-imports.md``.
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


def test_content_is_the_canonical_json_of_exactly_the_declared_keys() -> None:
    """Undeclared keys on the payload (``output``, ``steps``, ``driving_signals``, or anything
    else the harness carried) never leak into the saved document."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    payload = {
        "posture": "prerequisite",
        "headline": "needs clearer marketing strategy",
        "output": "the model's raw prose answer",
        "steps": [{"index": 0, "kind": "tool"}],
    }
    doc = build_document(payload=payload, declared_keys=["posture", "headline"], producer=_PRODUCER)

    assert json.loads(doc["content"]) == {
        "posture": "prerequisite",
        "headline": "needs clearer marketing strategy",
    }


def test_content_is_a_plain_string_never_structure() -> None:
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload={"posture": "prerequisite", "headline": "x"},
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    assert isinstance(doc["content"], str)


def test_source_type_is_always_text_even_with_nested_values() -> None:
    """The Neo4j map-property TypeError regression: a nested object among the declared keys must
    never push this off the ``text`` path."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    payload = {
        "headline": "needs clearer marketing strategy",
        "hypotheses": [
            {"text": "price is the blocker", "label": "hypothesis", "experiment": "survey"}
        ],
    }
    doc = build_document(
        payload=payload, declared_keys=["headline", "hypotheses"], producer=_PRODUCER
    )

    assert doc["source_type"] == "text"


def test_a_nested_value_survives_intact_inside_the_content_string() -> None:
    """The nested object is not dropped or flattened — it round-trips exactly, just carried as
    text rather than structure."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    hypotheses = [{"text": "price is the blocker", "label": "hypothesis", "experiment": "survey"}]
    payload = {"headline": "x", "hypotheses": hypotheses}
    doc = build_document(
        payload=payload, declared_keys=["headline", "hypotheses"], producer=_PRODUCER
    )

    assert json.loads(doc["content"])["hypotheses"] == hypotheses


def test_no_title_is_set() -> None:
    """``derive_name`` (KGS) falls back to the member role when no title is given — the platform
    write deliberately supplies none."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload={"posture": "prerequisite", "headline": "x"},
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    assert doc.get("title") is None


def test_the_producer_stamp_is_carried_through_verbatim() -> None:
    """The document mints no identity of its own — whatever six-field stamp the caller built (the
    same shape ``_producer_ref`` mints for the tool path) rides onto the document unchanged."""
    from oraclous_execution_engine_service.domain.member_artifact import build_document

    doc = build_document(
        payload={"posture": "prerequisite", "headline": "x"},
        declared_keys=["posture", "headline"],
        producer=_PRODUCER,
    )

    assert doc["producer"] == _PRODUCER
