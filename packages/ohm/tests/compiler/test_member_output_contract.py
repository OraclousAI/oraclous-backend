"""#697 — every compiled member DECLARES what it hands to the next one.

``outputs_schema`` + ``validate_payload`` have enforced a typed hand-off since ADR-035: a producer
that omits a key it declared fails fail-closed at the boundary. The contract is inert on every
compiled team, because the compiler never fills the declaration. Team run ``fe548aac`` is the
evidence: 14 members, ``outputs_schema: {}`` on all 14, four filing conventions invented in one
run, and an ``Editor`` that burned 34,855 tokens chasing two files that were never written.

Ruling (2026-08-24): every member declares its output keys, with NO exception for a member nobody
depends on — the narrower rule would make adding a ``depends_on`` edge silently change what an
earlier member must produce.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT
from oraclous_ohm.compiler.validate import validate_draft

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG = ["web-search", "graph-ingest"]


def _member(role: str, **over: object) -> dict:
    base: dict = {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "tools": ["web-search"],
        "depends_on": [],
        "outputs_schema": {"required": ["summary"]},
    }
    base.update(over)
    return base


def _draft(members: list[dict]) -> dict:
    return {"members": members}


def test_the_drafter_is_told_to_declare_output_keys_on_every_member() -> None:
    # The prompt is the only place the declaration can come from — the drafter emits the members.
    assert "outputs_schema" in DRAFTER_PROMPT
    lowered = DRAFTER_PROMPT.lower()
    assert "every member" in lowered  # the ruling's no-exception rule, stated to the model


def test_the_drafter_is_given_a_default_for_an_objective_that_says_nothing() -> None:
    # The ruling left this open for the implementer: emit a DEFAULT, never `{}`. `summary` is the
    # one key every member can always honour; `artifact_refs` is the key the EURail run needed and
    # did not have, so a member that persists something declares it too.
    assert "summary" in DRAFTER_PROMPT
    assert "artifact_refs" in DRAFTER_PROMPT


def test_a_member_that_declares_no_output_blocks_the_compile() -> None:
    # `outputs_schema: {}` is what every compiled team ships today. It must stop being valid.
    draft = _draft([_member("researcher", outputs_schema={}), _member("writer")])
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True, v


def test_the_undeclared_member_is_NAMED_so_the_reviewer_can_repair_it() -> None:
    # A blocking reason that names no member cannot be repaired: REVIEWER_PROMPT tells the reviewer
    # to "edit exactly the members/tools the blocking reasons name" (#751 is the same lesson).
    draft = _draft([_member("researcher"), _member("writer", outputs_schema={})])
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert any("writer" in b for b in v["blocking"]), v["blocking"]


def test_a_member_nobody_depends_on_still_has_to_declare() -> None:
    # The deliberate part of the ruling: a terminal member is not exempt, so adding a depends_on
    # edge later never silently changes what an earlier member must produce.
    terminal = _member("terminal", outputs_schema={})
    v = validate_draft(_draft([_member("first"), terminal]), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True, v
    assert any("terminal" in b for b in v["blocking"]), v["blocking"]


def test_a_declared_output_contract_passes() -> None:
    draft = _draft([_member("researcher"), _member("writer", depends_on=["researcher"])])
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False, v


def test_an_outputs_schema_declaring_no_keys_is_not_a_declaration() -> None:
    # `{"required": []}` reads as declared and enforces nothing — validate_payload returns no
    # errors for it. It must block like `{}`, or the gate is trivially satisfied.
    draft = _draft([_member("researcher", outputs_schema={"required": []}), _member("writer")])
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True, v


def test_a_member_added_by_a_hand_edit_is_not_rejected_by_the_new_rule() -> None:
    """The rule must not turn the user's own edit into a block.

    "Add a fact-checker" is one of the four typed edits a user can make to a compiled team, and the
    edit carries no place to state output keys. If the added member arrives with an empty
    declaration, the very next validation rejects a team the user just asked for. So the edit gets
    the same default the drafter emits.
    """
    from oraclous_ohm.compiler.refine import AddMember, apply_refine
    from oraclous_ohm.import_.setup import assemble_and_report
    from oraclous_ohm.manifest import OHMMember

    draft = _draft([_member("researcher"), _member("writer", depends_on=["researcher"])])
    built = assemble_and_report(
        "t",
        [OHMMember.model_validate(m) for m in draft["members"]],
        owner_organization_id=_ORG,
        shape="compiled",
    )
    manifest = built.manifest
    assert manifest is not None

    res = apply_refine(
        manifest,
        AddMember(role="fact-checker", tools=["web-search"], depends_on=["writer"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    added = {m.role: m for m in res.manifest.members}["fact-checker"]
    assert added.outputs_schema.get("required"), "a hand-added member declares nothing"

    # and the compile gate, which the refine endpoint re-runs, accepts the edited team
    edited = {"members": [m.model_dump(mode="json") for m in res.manifest.members]}
    v = validate_draft(edited, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False, v
