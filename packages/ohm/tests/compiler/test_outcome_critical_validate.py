"""#834 ruling §A.3 — a drafted member marked ``outcome_critical: true`` with no non-empty
``outputs_schema.required`` is refused on the compiler's draft path, exactly like every other
member-schema refusal ``validate_draft`` already surfaces (F-DRAFT-INVALID via the
``OHMMember.model_validate`` failure branch).

This is the SAME load-time cross-check pinned in ``test_outcome_critical_manifest.py`` against
``load_ohm`` directly — this file pins its surface on the reviewer's ``manifest-validate`` tool,
which is a drafted member's real-world path to that refusal (the reviewer never calls ``load_ohm``).

RED until the ``[impl]`` adds the cross-check.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.validate import validate_draft

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _draft_with_reviewer(outputs_schema: dict | None) -> dict:
    member: dict = {
        "role": "reviewer",
        "kind": "agent",
        "manifest_ref": "org:x/reviewer@1",
        "outcome_critical": True,
    }
    if outputs_schema is not None:
        member["outputs_schema"] = outputs_schema
    return {"members": [member]}


def test_outcome_critical_with_no_outputs_schema_blocks_f_draft_invalid() -> None:
    v = validate_draft(_draft_with_reviewer(None), ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-DRAFT-INVALID" in b for b in v["blocking"])


def test_outcome_critical_with_empty_required_blocks_f_draft_invalid() -> None:
    draft = _draft_with_reviewer({"required": []})
    v = validate_draft(draft, ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-DRAFT-INVALID" in b for b in v["blocking"])


def test_the_blocking_reason_names_the_reviewer_role() -> None:
    # #751: the reviewer's repair loop only has something to edit if the reason names the member.
    #
    # Scoped to the F-DRAFT-INVALID entry SPECIFICALLY (be-test-reviewer, PR #1016 blocker): this
    # same draft ALSO blocks today for an unrelated, pre-existing reason —
    # F-NO-OUTPUT-CONTRACT's own message is "member 'reviewer' declares no output keys", which
    # already contains "reviewer". An unscoped `any("reviewer" in b for b in v["blocking"])` would
    # pass on that message alone and never exercise this issue's own F-DRAFT-INVALID naming at all.
    v = validate_draft(_draft_with_reviewer(None), ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    draft_invalid = [b for b in v["blocking"] if "F-DRAFT-INVALID" in b]
    assert draft_invalid, "expected an F-DRAFT-INVALID entry for the outcome_critical refusal"
    assert any("reviewer" in b for b in draft_invalid)


def test_outcome_critical_with_a_non_empty_required_list_does_not_block_on_this_rule() -> None:
    draft = _draft_with_reviewer({"required": ["members"]})
    v = validate_draft(draft, ["web-search"], owner_organization_id=_ORG)
    # Scoped to THIS rule's own flag (be-test-reviewer, PR #1016) rather than the whole verdict —
    # decoupled from any other, unrelated flag a future check might add to this minimal draft,
    # which would otherwise flip `would_block` for a reason that has nothing to do with #834.
    assert not any("F-DRAFT-INVALID" in b for b in v["blocking"])
