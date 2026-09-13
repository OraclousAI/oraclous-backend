"""#594 — the reviewer's validate gate: one validator, capability-absence (ADR-032), JSON peel.

``validate_draft`` is the deterministic ``manifest-validate`` tool the reviewer's in-harness repair
loop calls — ``would_block`` is a coded boolean, never the model's opinion.
"""

from __future__ import annotations

import json
import uuid

import pytest
from oraclous_ohm.compiler.validate import validate_draft

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _draft(tool: str) -> dict:
    # #697: every member declares its output keys, so these fixtures carry the default
    # declaration. This file is about the capability-absence gate; the declaration is here only
    # so a draft that SHOULD pass is not blocked for an unrelated reason.
    return {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/r@1",
                "tools": [tool],
                "tool_rationale": {tool: f"needs {tool} to cover this sub-goal"},  # #718
                "outputs_schema": {"required": ["summary"]},
            },
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
                "outputs_schema": {"required": ["summary"]},
            },
        ]
    }


def test_a_surveyed_tool_passes() -> None:
    catalog = {"tools": [{"name": "web-search", "ref": "core/web-search@1"}]}
    v = validate_draft(_draft("web-search"), catalog, owner_organization_id=_ORG)
    assert v["would_block"] is False  # the tool resolves to the surveyed catalog → ready


def test_an_unsurveyed_tool_fails_closed() -> None:
    # the drafter hallucinated 'teleport' — not surveyed → blocked, never run (ADR-032)
    v = validate_draft(_draft("teleport"), ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in v["blocking"])
    assert "GO: BLOCKED" in v["report"]  # render_report surfaces the block to the reviewer


def test_an_empty_draft_fails_closed_no_members() -> None:
    # a draft with no members (a drafter that produced nothing) → F-NO-MEMBERS, never a crash
    v = validate_draft({"members": []}, ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-NO-MEMBERS" in b for b in v["blocking"])


def test_a_tool_written_as_a_full_ref_matches_the_surveyed_name() -> None:
    # the drafter may write the surveyed REF (core/web-search@1.0.0) instead of the bare name; both
    # normalise to the same slug, so a legitimate surveyed tool is NOT falsely blocked (the deployed
    # run caught this — gpt-4o-mini drafted the full ref).
    catalog = {"tools": [{"name": "web-search", "ref": "core/web-search@1.0.0"}]}
    v = validate_draft(_draft("core/web-search@1.0.0"), catalog, owner_organization_id=_ORG)
    assert v["would_block"] is False


@pytest.mark.parametrize(
    "bogus",
    [
        "evil/web-search",  # ascii-alnum namespace
        "😈/web-search",  # emoji namespace — _slug would erase it and collapse to 'web-search'
        ".../web-search",  # pure-punctuation namespace
        "___/web-search",  # underscores
        "中文/web-search",  # CJK namespace
        "ｃｏｒｅ/web-search",  # fullwidth 'core' — NOT the canonical ascii 'core/'
        "core//web-search",  # malformed double-slash
        "core/web-search/x",  # nested path
    ],
)
def test_a_bogus_namespace_tool_cannot_masquerade_as_a_surveyed_tool(bogus: str) -> None:
    # SECURITY: only the canonical ascii 'core/' namespace is stripped — NO other '/'-bearing form
    # may collapse to the surveyed bare slug and slip past the gate, however the namespace slugifies
    # (incl. emoji/punct/CJK that a bare _slug would erase entirely). Every variant blocks.
    catalog = {"tools": [{"name": "web-search", "ref": "core/web-search@1.0.0"}]}
    v = validate_draft(_draft(bogus), catalog, owner_organization_id=_ORG)
    assert v["would_block"] is True, f"{bogus!r} masqueraded as the surveyed web-search"
    assert any("F-CAPABILITY-MISSING" in b for b in v["blocking"])


def test_a_full_ref_with_the_wrong_slug_is_blocked() -> None:
    # core/web-search@1.0.0 must NOT match a 'web-research' catalog — a different slug is a miss.
    v = validate_draft(
        _draft("core/web-search@1.0.0"), ["web-research"], owner_organization_id=_ORG
    )
    assert v["would_block"] is True


def test_an_empty_slug_tool_and_catalog_entry_both_fail_closed() -> None:
    # MEDIUM: an empty-slug catalog entry ('') must NOT become a wildcard, and an empty-slug drafted
    # tool ('@', '/') must itself block — never a silent pass through "" == "".
    v = validate_draft(_draft("@"), ["", "web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in v["blocking"])


def test_two_distinct_erasing_namespaces_do_not_collide() -> None:
    # SECURITY (2nd review round): an erasing-namespace catalog entry ('./web-research', e.g. from a
    # poisoned relayed catalog) must NOT admit a DIFFERENT erasing-namespace drafted tool
    # ('/web-research') — both are degenerate and fail closed, never collapsing onto one slug.
    v = validate_draft(
        _draft("/web-research"), {"tools": [{"name": "./web-research"}]}, owner_organization_id=_ORG
    )
    assert v["would_block"] is True


def test_a_legit_foreign_namespace_matches_only_its_own_form() -> None:
    # a non-core namespaced tool matches the SAME form (a/web-research ↔ a/web-research) but a
    # distinct namespace stays distinct (b/web-research would NOT match) — identity preserved.
    assert (
        validate_draft(_draft("a/web-research"), ["a/web-research"], owner_organization_id=_ORG)[
            "would_block"
        ]
        is False
    )
    assert (
        validate_draft(_draft("b/web-research"), ["a/web-research"], owner_organization_id=_ORG)[
            "would_block"
        ]
        is True
    )


def test_prose_wrapped_json_is_peeled_not_misblocked() -> None:
    # a REAL drafter LLM wraps the JSON in prose / a ```json fence — it must still parse (#599)
    draft = (
        "Here is the team you asked for:\n```json\n" + json.dumps(_draft("web-search")) + "\n```\n"
    )
    v = validate_draft(draft, ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is False  # peeled + validated, NOT F-DRAFT-INVALID


def test_garbage_fails_closed() -> None:
    v = validate_draft(
        "Sorry, I could not build a team.", ["web-search"], owner_organization_id=_ORG
    )
    assert v["would_block"] is True
    assert any("F-DRAFT-INVALID" in b for b in v["blocking"])


def test_a_team_json_followed_by_a_separate_receipt_object_is_still_peeled() -> None:
    # #1043 — validate_draft's peel is the SAME greedy regex that breaks on a real reviewer reply:
    # the drafted team JSON followed by a SEPARATE `driving_signals` receipt object (the
    # REVIEWER_PROMPT-mandated shape). RED until the [impl] fixes the peel to find the team object
    # even when a second JSON object trails it.
    receipt = {"driving_signals": [{"signal": "ok", "value": True, "source_tool_call_id": "c1"}]}
    draft = json.dumps(_draft("web-search")) + "\n\n" + json.dumps(receipt)
    v = validate_draft(draft, ["web-search"], owner_organization_id=_ORG)
    assert v["would_block"] is False, v  # peeled + validated, NOT F-DRAFT-INVALID


# ── #900: the catalogue check and the free-text fallback are UNCHANGED — explicit regression pin ──
#
# #900 gives the manifest-drafter member its own tool call (`draft-manifest`) as its structured
# answer mechanism, so a drafted team commonly carries a different tool shape than it did before.
# The issue's acceptance criteria say plainly: "the existing fail-closed catalogue check must stay
# UNCHANGED. It blocks any tool not in the surveyed catalogue. This issue reduces how often that
# check fires; it never replaces it" — and separately that the free-text fallback (a model that
# answers in prose anyway) stays. Neither test below is a bug hunt: nothing in validate.py changes
# for #900, so both are expected to PASS today. They exist so that a LATER [impl] PR for #900
# cannot silently weaken or delete either path (e.g. by reasoning "the drafter calls a real tool
# now, the free-text catalogue diff is redundant") without turning this file red immediately.


def test_900_catalogue_check_still_blocks_a_hallucinated_tool_beside_a_real_one() -> None:
    """#900 acceptance-criterion regression pin, not a bug hunt — expected GREEN today.

    Pins the SAME coded verdict shape (``would_block: True``, ``F-CAPABILITY-MISSING`` naming the
    blocking tool) the existing single-tool tests already cover, but against a more realistic
    POST-#900 draft: a member holding a real surveyed tool ALONGSIDE a hallucinated one, the mixed
    shape #900 makes common once a drafted team's members carry more than one tool each. A future
    [impl] PR that weakens or removes the catalogue-absence check must turn this red immediately.
    """
    catalog = {"tools": [{"name": "web-search", "ref": "core/web-search@1"}]}
    draft = {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/r@1",
                "tools": ["web-search", "teleport"],
                "tool_rationale": {
                    "web-search": "needs web-search to cover this sub-goal",
                    "teleport": "needs teleport to cover this sub-goal",
                },
                "outputs_schema": {"required": ["summary"]},
            },
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
                "outputs_schema": {"required": ["summary"]},
            },
        ]
    }
    v = validate_draft(draft, catalog, owner_organization_id=_ORG)
    assert v["would_block"] is True  # 'teleport' is not surveyed — blocks despite 'web-search'
    assert any("F-CAPABILITY-MISSING" in b for b in v["blocking"])
    assert "GO: BLOCKED" in v["report"]  # render_report still surfaces the block to the reviewer


def test_900_prose_wrapped_draft_with_the_new_drafter_and_reviewer_tools_still_validates() -> None:
    """#900 acceptance-criterion regression pin, not a bug hunt — expected GREEN today.

    Pins that the free-text fallback stays: the greedy text-scraping bug this issue's brief
    complains about was already fixed by #1043 (51c054cd, an ancestor of this branch's base) —
    ``validate_draft`` now peels the FIRST well-formed JSON object with a forward-scanning
    ``json.JSONDecoder().raw_decode``, not a greedy regex. Rather than duplicate the existing
    ``test_prose_wrapped_json_is_peeled_not_misblocked`` pin verbatim, this ties the same fallback
    path to #900 directly: the wrapped JSON team carries the new ``manifest-drafter`` role holding
    its ``draft-manifest`` tool call next to a ``manifest-validate``-tooled ``reviewer`` role
    (see ``packages/ohm/src/oraclous_ohm/compiler/team.py``) — the exact shape #900 introduces.
    """
    catalog = {"tools": [{"name": "draft-manifest"}, {"name": "manifest-validate"}]}
    draft_dict = {
        "members": [
            {
                "role": "manifest-drafter",
                "kind": "agent",
                "manifest_ref": "org:x/d@1",
                "tools": ["draft-manifest"],
                "tool_rationale": {
                    "draft-manifest": "needs draft-manifest to emit its structured answer"
                },
                "outputs_schema": {"required": ["summary"]},
            },
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:x/rv@1",
                "depends_on": ["manifest-drafter"],
                "tools": ["manifest-validate"],
                "tool_rationale": {
                    "manifest-validate": "needs manifest-validate to check the draft"
                },
                "outputs_schema": {"required": ["summary"]},
            },
        ]
    }
    draft = "Here is the compiled team:\n```json\n" + json.dumps(draft_dict) + "\n```\nLet me know!"
    v = validate_draft(draft, catalog, owner_organization_id=_ORG)
    assert v["would_block"] is False, v  # peeled by raw_decode + validated, NOT F-DRAFT-INVALID
