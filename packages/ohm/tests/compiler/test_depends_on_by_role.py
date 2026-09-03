"""#751 — dependencies are declared by ROLE NAME, and a bad one says which member and which field.

Compiler run ``2d24b128`` produced no team at all. The drafter wrote ``"depends_on": [1, 2, 3, 4]``
— positional indices — because ``DRAFTER_PROMPT`` shows the field as a bare ellipsis and states
only that the edges must be acyclic, never that the elements are role names. Every other field in
that template is named or shown with a concrete value; this one is not.

The second half is worse than the first. ``validate_draft`` catches the Pydantic error and reports
``F-DRAFT-INVALID: a draft member failed schema validation`` — no member, no field. The reviewer is
told to "edit exactly the members/tools the blocking reasons name", so it had nothing to act on: it
re-validated five times, got a byte-identical verdict each time, and exhausted its cap at 24,050
tokens. The Pydantic error already said ``members.4.depends_on.0: Input should be a valid string``.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT
from oraclous_ohm.compiler.validate import validate_draft

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG = ["web-search"]


def _member(role: str, **over: object) -> dict:
    base: dict = {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "tools": ["web-search"],
        "tool_rationale": {"web-search": "needs live results to answer the objective"},  # #718
        "depends_on": [],
        "outputs_schema": {"required": ["summary"]},
    }
    base.update(over)
    return base


def test_the_drafter_template_shows_depends_on_with_role_names() -> None:
    # An ellipsis is not a shape. The template must carry a concrete role-name example, the way
    # every other field in it already does.
    assert '"depends_on":[…]' not in DRAFTER_PROMPT
    assert '"depends_on": […]' not in DRAFTER_PROMPT
    lowered = DRAFTER_PROMPT.lower()
    assert "role name" in lowered  # the rule, stated
    assert "index" in lowered or "position" in lowered  # and what it is NOT


def test_a_positional_depends_on_names_the_member_and_the_field() -> None:
    # The exact draft from run 2d24b128, reduced. The reviewer's repair loop needs both halves.
    draft = {"members": [_member("researcher"), _member("synthesizer", depends_on=[0])]}
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True
    joined = " ".join(v["blocking"])
    assert "synthesizer" in joined or "members.1" in joined, v["blocking"]
    assert "depends_on" in joined, v["blocking"]


def test_the_generic_sentence_is_gone() -> None:
    # "a draft member failed schema validation" names nothing; it is what made run 2d24b128
    # unrepairable. A named reason may still carry the code, but not that bare sentence alone.
    draft = {"members": [_member("synthesizer", depends_on=[1])]}
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    bare = "F-DRAFT-INVALID: a draft member failed schema validation"
    assert bare not in v["blocking"], v["blocking"]


def test_every_malformed_member_is_reported_not_only_the_first() -> None:
    # validate_draft returns on the FIRST bad member, so a draft with two typos costs two compiles.
    # The reviewer may fix at most twice; it should be able to fix both in one pass.
    draft = {
        "members": [
            _member("synthesizer", depends_on=[0]),  # a dependency by position
            _member("approver", kind="human"),  # a human member with no human_role
            _member("writer"),
        ]
    }
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True
    joined = " ".join(v["blocking"])
    assert "synthesizer" in joined and "approver" in joined, v["blocking"]


def test_a_valid_role_name_dependency_still_passes() -> None:
    draft = {"members": [_member("researcher"), _member("writer", depends_on=["researcher"])]}
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False, v
