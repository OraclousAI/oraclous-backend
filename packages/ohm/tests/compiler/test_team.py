"""#594 — the compiler is a 3-member ACYCLIC team (no loop-SCC); the reviewer holds the validator.

CTO decision A: the repair loop is the reviewer's IN-HARNESS tool-use loop, so the team itself is a
plain linear chain with NO team-level loop and NO engine done-check.

#709 deleted the capability-surveyor step: its only job was retyping the surveyed catalog as its
own output, and nothing read that output any more — the drafter gets the described catalog baked
into its own sub-goal (#713), and the reviewer's manifest-validate reads the org's live registry
directly (#705). The chain is now planner -> manifest-drafter -> reviewer.
"""

from __future__ import annotations

import uuid
from typing import Any

import jsonschema
import pytest
from oraclous_ohm.compiler.team import build_compiler_team
from oraclous_ohm.compiler.validate import _catalog_slugs
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")

# ── #900 (ADR-053) helpers ───────────────────────────────────────────────────────────────────
# The drafter's structured answer is now a real tool call — ``draft-manifest`` (a new, keyless
# in-process connector shaped exactly like ``ManifestValidatePlugin``, built in a LATER commit in
# services/capability-registry-service — not this one; the tests below only need its NAME, since
# ``build_compiler_team`` references tools by binding string, never by importing the connector).
# ``OHMCapability.resolved_schema`` (ADR-053 decision 1) and ``OHMMember.answer_from_tool``
# (decision 2) are pinned at the FIELD level in ``packages/ohm/tests/test_resolved_schema_manifest
# .py`` / ``test_answer_from_tool_manifest.py`` already — the tests below pin ``build_compiler_
# team``'s USE of those fields for the manifest-drafter specifically, a different scope.


def _drafter_capability(subs: dict[str, dict], binding: str) -> dict | None:
    """The capability dict on the manifest-drafter's sub-harness carrying this binding, or None."""
    caps = subs.get("manifest-drafter", {}).get("capabilities", [])
    for cap in caps:
        if isinstance(cap, dict) and cap.get("binding") == binding:
            return cap
    return None


def _find_enum_lists(node: object) -> list[list[object]]:
    """Recursively collect every JSON-Schema ``enum`` array found anywhere in a schema dict."""
    found: list[list[object]] = []
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list):
            found.append(enum)
        for value in node.values():
            found.extend(_find_enum_lists(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_enum_lists(item))
    return found


def _all_string_values(node: object) -> set[str]:
    """Every plain string appearing ANYWHERE in a JSON value, recursively — proves a catalogue
    name does not leak into a schema by any route (not merely that one particular key is absent)."""
    values: set[str] = set()
    if isinstance(node, dict):
        for value in node.values():
            values |= _all_string_values(value)
    elif isinstance(node, list):
        for item in node:
            values |= _all_string_values(item)
    elif isinstance(node, str):
        values.add(node)
    return values


def _assert_top_level_closed_and_fully_required(schema: dict) -> None:
    """Assert the schema's OWN top-level object is CLOSED (``additionalProperties: False``) and
    EVERY top-level property key also appears in ``required``.

    Scoped to the TOP LEVEL ONLY — corrected 2026-09-13 against #898's real, measured renderer
    behaviour (``render_strict_schema`` in ``tool_schemas.py``, landed on ``main`` via its [tests]
    PRs #1057/#1060 and its [impl] PR #1059). Two things drive this narrowing, not a relaxation of
    rigor:

    1. The provider-probe fact that actually matters here does NOT generalise the way an earlier
       draft of this test assumed: nesting a partial ``required`` list on an object INSIDE an
       array still fully constrained the model (0/5 forbidden values, same as a complete list) —
       ``render_strict_schema`` deliberately leaves a nested object's own ``required`` untouched
       (see #898's ``test_additional_properties_is_closed_on_a_nested_object_inside_an_array``:
       "the nested object's OWN required list is untouched"). Asserting full-required at every
       nested level here would pin a rule the platform does not actually enforce or need.
    2. #898 already closes ``additionalProperties`` on nested objects/array-items generically, at
       render time, from ANY schema whose op carries the explicit ``parameters_schema_strict``
       marker — #900 does not need to pre-close nested structures itself for that renderer to do
       its job; asserting it here would test the RENDERER's behaviour, not the compiler's.

    What #900's own authored schema MUST get right, and the renderer will NOT fix for us: every
    TOP-LEVEL property must be genuinely ``required`` from the start, not left optional. The
    renderer widens a genuinely-optional top-level property to a NULLABLE type instead of leaving
    it out (fact 2 of #898's docstring) — which would let the model satisfy our tool-choice field
    with ``null``, exactly the fail-open this whole issue exists to close. So the enum-constrained
    field(s) here must be required by the compiler's own construction, not by relying on the
    renderer's forced-required-and-nullable fallback.
    """
    properties = schema.get("properties")
    assert isinstance(properties, dict) and properties, "expected a non-empty top-level properties"
    assert schema.get("additionalProperties") is False, (
        "top-level additionalProperties must be False"
    )
    required = schema.get("required")
    assert isinstance(required, list), "top-level required must be a list"
    missing = set(properties) - set(required)
    assert not missing, f"top-level properties {sorted(missing)} are missing from required"


_CATALOG_SMALL = [
    {"name": "alpha-tool", "description": "First surveyed tool."},
    {"name": "beta-tool", "description": "Second surveyed tool."},
    {"name": "gamma-tool"},
]

#: The real ceiling constant is [impl]'s call — not pinned here (see module-level bullet in the
#: issue brief). A fresh org measured 11 tools, a fully seeded deployed org 21, a large import
#: lands near 80-100 — this assumes a plausible ceiling of 100 SOLELY to exercise the AT-ceiling /
#: ONE-above-ceiling boundary; if [impl] picks a different number these two tests need their
#: assumed constant updated to match, but the BEHAVIOR they pin (present+complete at the ceiling;
#: absent, with no leaked subset, one above it) does not change.
_ASSUMED_CEILING = 100


def _catalog_of(n: int) -> list[dict]:
    return [{"name": f"surveyed-tool-{i:04d}"} for i in range(n)]


def test_the_compiler_team_assembles_linear_and_acyclic() -> None:
    # #709: the capability-surveyor step is DELETED — nothing reads its output any more (the
    # drafter gets the described catalog straight from its own sub-goal, and the reviewer's
    # manifest-validate reads the org's live registry, never the surveyor's retyped list). The
    # chain is now planner -> manifest-drafter -> reviewer, three stages, not four.
    manifest, _subs = build_compiler_team(_ORG)
    loaded = load_ohm(manifest.model_dump(mode="json"))  # THE REAL loader
    assert loaded.is_team()
    assert loaded.execution_stages() == [
        ["planner"],
        ["manifest-drafter"],
        ["reviewer"],
    ]
    # CTO decision A: NO team-level loop — the repair is the reviewer's own in-harness loop
    assert not (loaded.orchestration and loaded.orchestration.loops)


def test_the_reviewer_holds_the_validate_tool_drafter_holds_draft_manifest() -> None:
    """#900 (ADR-053 decisions 1+2): the drafter's structured answer is now enforced by a real
    tool call — ``draft-manifest`` — mirroring the reviewer's existing ``manifest-validate``
    precedent (the reviewer is precisely the earlier case of a compiler member holding a real
    tool). Only the planner is left reasoning-only now, so this test's OLD name/claim ("others are
    reasoning only") no longer holds for the drafter; corrected here rather than left to rot
    silently (test-author owns this correction, not the implementer — CLAUDE.md 4.1)."""
    manifest, subs = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].tools == ["manifest-validate"]  # the in-harness repair calls validate
    assert by["manifest-drafter"].tools == ["draft-manifest"]  # #900: its tool call IS its answer
    assert by["planner"].tools == []  # the only member left reasoning-only
    assert set(subs) == {"planner", "manifest-drafter", "reviewer"}  # #709: no surveyor


def test_the_budget_is_the_three_layer_shape() -> None:
    manifest, _ = build_compiler_team(_ORG)
    b = manifest.budget
    assert b is not None
    assert b.max_tokens_total == 200_000 and b.max_sub_runs == 20  # the team pool (enforced axes)
    assert b.max_tokens_per_member == 60_000 and b.max_tokens_per_member <= b.max_tokens_total


def test_the_reviewer_repair_loop_is_hard_bounded_to_n_attempts() -> None:
    # CTO decision A / decision-3: the reviewer's in-harness validate→FIX→validate loop is bounded.
    # Each attempt is one manifest-validate call, so the bound is HARD-enforced by capping the
    # reviewer's max_tool_calls at _REPAIR_ATTEMPTS + 1 (the initial validate + at most N fixes) —
    # resolve_member_caps → the harness halts the loop at the cap regardless of the prompt. So a
    # persistently-blocked draft fail-closes after exactly N attempts; a draft needing ≤N repairs
    # converges within the cap.
    from oraclous_ohm.compiler.team import (
        _REPAIR_ATTEMPTS,
        _REVIEWER_OVERCHECK_SLACK,
        _REVIEWER_VALIDATE_CALLS,
    )

    assert _REPAIR_ATTEMPTS == 2  # default 2 fixes (CTO: default 2 / max 3)
    # #596: the HARD cap is the repair budget PLUS weak-model over-check slack — still bounded (no
    # runaway), but a clean compile no longer degrades because the model re-checked a passing draft.
    assert _REVIEWER_VALIDATE_CALLS == _REPAIR_ATTEMPTS + 1 + _REVIEWER_OVERCHECK_SLACK
    assert _REVIEWER_VALIDATE_CALLS > _REPAIR_ATTEMPTS + 1  # carries explicit over-check slack
    manifest, _ = build_compiler_team(_ORG)
    by = {m.role: m for m in manifest.members}
    assert by["reviewer"].max_tool_calls == _REVIEWER_VALIDATE_CALLS  # the harness halts here
    # only the reviewer is tool-call-bounded; the others are reasoning-only (no tools, no loop)
    assert all(by[r].max_tool_calls is None for r in ("planner", "manifest-drafter"))


def test_the_objective_is_seeded_into_the_planner_subgoal() -> None:
    # slice-1: the prose objective → the planner's subgoal. No engine wiring — it renders as the
    # member's harness Objective: line (team_run._render_input). #709: there is no surveyor left
    # to seed a catalog into; the described catalog reaching the drafter is
    # test_catalog_descriptions.py's job, not this test's.
    manifest, _ = build_compiler_team(_ORG, objective="Summarise the week's AI news into a digest.")
    by = {m.role: m for m in manifest.members}
    # the objective LEADS the planner's subgoal (followed by the seed topology shapes, #596)
    assert by["planner"].subgoal and by["planner"].subgoal.startswith(
        "Summarise the week's AI news into a digest."
    )
    assert "capability-surveyor" not in by  # #709: the step is gone


def test_the_drafter_is_seeded_to_emit_the_governance_policy() -> None:
    # #596: the drafter's subgoal seeds the governed-by-default policy template so the compiled team
    # carries governance (a known policy_set_ref) + the 3-layer budget.
    from oraclous_ohm.seeds import DEFAULT_POLICY_SET_REF

    manifest, _ = build_compiler_team(_ORG)
    drafter = {m.role: m for m in manifest.members}["manifest-drafter"]
    assert drafter.subgoal and DEFAULT_POLICY_SET_REF in drafter.subgoal
    assert "max_tokens_per_member" in drafter.subgoal  # the 3-layer budget is seeded


def test_the_planner_composes_from_the_seed_reference_topologies() -> None:
    # #596 DoD item 3 (CTO blocker fix): the planner's subgoal seeds the reference topology shape
    # names so it COMPOSES FROM them (never a frozen pipeline); the prose objective leads.
    manifest, _ = build_compiler_team(_ORG, objective="summarise the week's news")
    planner = {m.role: m for m in manifest.members}["planner"]
    assert planner.subgoal and "summarise the week's news" in planner.subgoal
    for shape in ("fan-out-fan-in", "standing-team", "gated-pipeline"):
        assert shape in planner.subgoal, f"the seed shape {shape!r} is seeded into the planner"


# ── the reviewer prompt must not fight the run's grounding directive ─────────


def test_reviewer_prompt_asks_for_the_grounding_receipt_alongside_the_team() -> None:
    """The reviewer declares ``manifest-validate``, so the engine appends GROUNDING_DIRECTIVE to
    its input and grades it on a ``driving_signals`` receipt (#642). This prompt used to answer
    that with "Reply IMMEDIATELY with ONLY that team JSON ... STOP", i.e. two instructions the
    model cannot both obey — so it satisfied one at random and compiles failed on a coin flip
    (run ``afc3b2c4``: 3 ok manifest-validate calls, a valid manifest, no receipt -> the member
    failed for unbacked claims; run ``8097a667``: receipt present -> the draft peel choked on it).
    Both instructions have to be satisfiable at once.
    """
    from oraclous_ohm.compiler.prompts import REVIEWER_PROMPT

    assert "driving_signals" in REVIEWER_PROMPT
    # the receipt is additive to the team JSON, never a replacement for it
    assert "BOTH required" in REVIEWER_PROMPT
    # and the exclusive phrasing that forbade it must not come back
    assert "ONLY that team JSON" not in REVIEWER_PROMPT


# ── #718: the drafter must justify every tool it hands out ──────────────────


def test_drafter_prompt_requires_a_tool_rationale_entry_per_tool() -> None:
    """The gate (validate.py's F-TOOL-UNJUSTIFIED) blocks a member that holds a tool with no stated
    reason, so the drafter has to be TOLD to fill `tool_rationale` before it ever gets there — the
    JSON example shows the field alongside `tools`, and a RULES bullet requires one entry per
    assigned tool."""
    from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT

    assert '"tool_rationale"' in DRAFTER_PROMPT  # the JSON example shows the field
    assert DRAFTER_PROMPT.count("tool_rationale") >= 2  # the example AND a rule about it
    assert "this member" in DRAFTER_PROMPT.lower()  # ties the reason to THIS member, not the tool


def test_drafter_prompt_discourages_leaving_tools_empty_when_one_fits() -> None:
    """#718's companion team-level check (F-TEAM-NO-TOOLS) is confirm-severity, not blocking — so
    the only real lever against a team that hands out zero tools while the catalog offers one that
    fits is a soft nudge in the prompt itself."""
    from oraclous_ohm.compiler.prompts import DRAFTER_PROMPT

    assert "tools: []" in DRAFTER_PROMPT or "empty" in DRAFTER_PROMPT.lower()


# ── #900 (ADR-053): the drafter's tool call IS its answer ────────────────────────────────────


def test_the_manifest_drafter_holds_draft_manifest_as_its_answer_tool() -> None:
    """ADR-053 decisions 1+2: the drafter's structured answer is enforced by the platform's own
    tool-calling machinery, never free text the platform tries to peel JSON out of (#1043's class
    of failure — a compliant model still failing shape validation). RED today: ``tools`` is ``[]``
    on ``main`` and ``answer_from_tool`` does not exist on ``OHMMember`` at all yet."""
    manifest, _subs = build_compiler_team(_ORG)
    drafter = {m.role: m for m in manifest.members}["manifest-drafter"]
    assert drafter.tools == ["draft-manifest"]
    assert drafter.answer_from_tool == "draft-manifest"


def test_the_manifest_drafter_subharness_carries_the_draft_manifest_capability() -> None:
    """Mirrors the reviewer's own existing precedent: ``_sub("reviewer", REVIEWER_PROMPT,
    [_VALIDATE_TOOL])`` gives the reviewer's sub-harness a real ``manifest-validate`` capability
    via ``build_subharness``'s ``tools`` param, which synthesises the ref ``core/manifest-
    validate@1`` (``_capability_ref`` in ``import_/mapping.py``, since ``"manifest-validate"`` is
    not one of the graph-substrate-remapped file tools). The drafter's sub-harness must carry the
    same shape for ``draft-manifest``, synthesising ``core/draft-manifest@1``. RED today: the
    drafter's sub-harness is built with ``tools=[]``, so it carries no capability at all."""
    _manifest, subs = build_compiler_team(_ORG)
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None, "manifest-drafter sub-harness must carry a draft-manifest capability"
    assert cap["ref"] == "core/draft-manifest@1"


def test_the_drafter_capability_resolved_schema_constrains_tool_names_in_menu_order() -> None:
    """The schema's enum-of-tool-names, when present, lists the surveyed catalog's names in the
    SAME ORDER as ``catalog_descriptions`` (menu order) — the exact overall schema shape beyond
    this enum is [impl]'s design choice (not pinned here); this test recurses the whole schema
    looking for ANY JSON-Schema ``enum`` array, so it does not assume a specific key path. RED
    today: ``resolved_schema`` does not exist on ``OHMCapability`` yet, so no capability is even
    minted for the drafter (``tools=[]`` on ``main``)."""
    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=_CATALOG_SMALL)
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None
    schema = cap.get("resolved_schema")
    assert schema is not None, "a surveyed catalog must produce a resolved_schema"
    names = [entry["name"] for entry in _CATALOG_SMALL]
    enums = _find_enum_lists(schema)
    assert any(e == names for e in enums), (
        f"expected an enum constraint listing the catalog's tool names in menu order {names}; "
        f"found enum(s): {enums}"
    )


def test_at_the_ceiling_the_allowed_value_list_is_present_and_complete() -> None:
    """At exactly the ceiling, the allowed-value list is present and complete — none dropped. The
    exact ceiling constant is [impl]'s call (see ``_ASSUMED_CEILING`` above); this pins the
    BEHAVIOR at a plausible assumed value. RED today (no ``resolved_schema`` exists at all)."""
    catalog = _catalog_of(_ASSUMED_CEILING)
    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=catalog)
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None
    schema = cap.get("resolved_schema")
    assert schema is not None
    names = [entry["name"] for entry in catalog]
    enums = _find_enum_lists(schema)
    assert any(e == names for e in enums), "at the ceiling the enum must be present and complete"


def test_one_above_the_ceiling_the_allowed_value_key_is_absent_and_no_name_leaks() -> None:
    """One entry above the ceiling: the enum/allowed-value key is ABSENT ENTIRELY (never truncated
    to a subset — the single most emphasized invariant in the issue brief) — and separately, NO
    catalogue name survives ANYWHERE in the schema by any other route, which is what actually
    catches a future "just truncate it" shortcut. The ``tools``/capability-presence assertions
    below are what make this test genuinely RED today (a not-yet-built ``resolved_schema`` would
    otherwise satisfy "absent" vacuously); the enum/leak assertions only start to bite once
    ``resolved_schema`` exists, and they are what protect against a bad implementation of the
    ceiling specifically."""
    catalog = _catalog_of(_ASSUMED_CEILING + 1)
    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=catalog)
    drafter = {m.role: m for m in manifest.members}["manifest-drafter"]
    assert drafter.tools == ["draft-manifest"]  # unconditional regardless of catalog size
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None
    schema = cap.get("resolved_schema")
    names = {entry["name"] for entry in catalog}
    if schema is not None:
        enums = _find_enum_lists(schema)
        assert not any(
            set(e) & names
            for e in enums
            if isinstance(e, list) and all(isinstance(v, str) for v in e)
        ), "no subset of the over-ceiling catalog may survive into any enum"
        leaked = names & _all_string_values(schema)
        assert not leaked, f"catalog names must never leak into the schema by any route: {leaked}"


def test_the_resolved_schema_closes_and_requires_every_top_level_property() -> None:
    """Measured, live-verified driving fact behind this issue: a closed enum only actually
    constrains a model's output when EVERY property in the constrained object's OWN schema also
    appears in that object's own ``required`` list — a partial ``required`` silently leaves the
    flag inert. Scoped to the TOP LEVEL only — see ``_assert_top_level_closed_and_fully_required``'s
    docstring for why nesting does not need (and must not be asserted to need) the same treatment;
    that is #898's ``render_strict_schema``'s job at render time, not this compiler's authoring
    job. RED today (no ``resolved_schema`` exists on ``OHMCapability`` at all)."""
    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=_CATALOG_SMALL)
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None
    schema = cap.get("resolved_schema")
    assert schema is not None
    _assert_top_level_closed_and_fully_required(schema)


def test_no_catalogue_still_gives_the_drafter_its_tool_but_never_a_zero_value_enum() -> None:
    """With no surveyed catalog (the unit-test default path, or a registry outage degrading to the
    seed inventory — see ``build_compiler_team``'s own docstring), the drafter still needs the
    ``draft-manifest`` TOOL — ``tools``/``answer_from_tool`` do not depend on having a catalog to
    enumerate. Only the per-tool-name enum constraint depends on having entries to enumerate.

    JUDGMENT CALL (flagged for correction if wrong): whether ``resolved_schema`` is built AT ALL
    with zero catalog entries is left to [impl] — what is pinned here is only that it must never
    FALSELY restrict a field to an empty legal-value set (an ``"enum": []`` would make that field
    impossible for ANY model, including a correct one, to ever fill)."""
    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=None)
    drafter = {m.role: m for m in manifest.members}["manifest-drafter"]
    assert drafter.tools == ["draft-manifest"]
    assert drafter.answer_from_tool == "draft-manifest"
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None
    schema = cap.get("resolved_schema")
    if schema is not None:
        enums = _find_enum_lists(schema)
        assert all(len(e) > 0 for e in enums), "an enum must never be emitted empty"


def test_the_schema_enum_is_a_subset_of_what_the_reviewers_own_catalog_check_allows() -> None:
    """Regression-safety pin, per the issue brief's own framing: the menu the drafting step sees
    and the catalog check the reviewer's ``manifest-validate`` enforces (``validate.py``'s
    ``_catalog_slugs``) already apply the SAME filter, deliberately — there is no bug to find here
    today. Still worth one test that the drafter's allowed-value list is a subset-or-equal of
    whatever ``_catalog_slugs`` would separately allow, fed the SAME ``catalog_descriptions``, so a
    future divergence between the two is caught. Currently RED simply because ``resolved_schema``
    doesn't exist yet to compare against — expected to become a live regression guard once #900
    lands, not to find a bug today."""
    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=_CATALOG_SMALL)
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None
    schema = cap.get("resolved_schema")
    assert schema is not None
    allowed = _catalog_slugs(_CATALOG_SMALL)
    names = [entry["name"] for entry in _CATALOG_SMALL]
    enums = _find_enum_lists(schema)
    matching = [e for e in enums if e == names]
    assert matching, "expected the menu-order enum this file's own earlier test pins to exist"
    for enum_list in matching:
        assert set(enum_list) <= allowed, "the enum must never exceed what the catalog check allows"


def test_planner_and_reviewer_are_unaffected_when_a_catalog_is_given() -> None:
    """#900 scopes the answer-from-tool change to the manifest-drafter alone. Same tools, same
    ceilings, same settings as today for the planner and the reviewer — a straightforward
    regression pin. Likely GREEN already: ``build_compiler_team``'s planner/reviewer construction
    is untouched by this issue, confirming the change is scoped to the drafter alone."""
    from oraclous_ohm.compiler.team import _REVIEWER_VALIDATE_CALLS

    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=_CATALOG_SMALL)
    by = {m.role: m for m in manifest.members}
    assert by["planner"].tools == []
    assert by["reviewer"].tools == ["manifest-validate"]
    assert by["reviewer"].max_tool_calls == _REVIEWER_VALIDATE_CALLS
    assert by["reviewer"].on_exhaustion == "degrade"
    assert by["reviewer"].outcome_critical is True
    assert subs["planner"]["capabilities"] == []
    assert [c["binding"] for c in subs["reviewer"]["capabilities"]] == ["manifest-validate"]


# ── the live regression this file's own shape-only tests could not catch (PR #1065 review) ──────
#
# The CTO's PR #1065 review comment (2026-09-13) recorded a real, live compile failure: an earlier
# revision of ``_drafter_resolved_schema`` closed the TOP-LEVEL object (this file's own
# ``_assert_top_level_closed_and_fully_required``) but left each drafted MEMBER's own object with
# no ``required`` list and no ``additionalProperties: False``. A real model answered with a member
# carrying only 3 of its 11 declared fields — ``role``, ``subgoal``, ``tools`` — and the compile
# died: the reviewer's repair prompt covers a hallucinated TOOL, not a missing STRUCTURAL field.
#
# The qa-engineer review on the same PR proved, independently, that NOTHING in this file's existing
# suite would have caught it: checking out the pre-fix commit and re-running all four unit suites
# gave 3569 passed, 0 failed — identical to the post-fix run. Every assertion here is scoped to the
# top-level object (deliberately, per ``_assert_top_level_closed_and_fully_required``'s own
# docstring); nothing inspects the MEMBER object's own ``required``/``additionalProperties``.
#
# So this test does not add another shape assertion — that is exactly the kind of test that failed
# to catch the incident. It takes the incident's own payload shape (the 3 fields the CTO's comment
# named — the literal captured draft was not preserved in the issue thread, so this reconstructs it
# field-for-field from that comment rather than inventing a fresh hypothetical) and asks a REAL JSON
# Schema validator whether the schema this function actually produces accepts it. The broken
# revision accepted it silently (qa-engineer reran this exact check against it: silently accepted).
# The fixed one must reject it, naming what is missing.

#: The incident payload, reconstructed field-for-field from the CTO's PR #1065 review comment
#: (2026-09-13): "the schema constrained the outer object but left each team member's own object
#: with no required list and no closure, so the model emitted three of eleven fields and the
#: compile died" — the three named fields, and nothing else.
_INCIDENT_MEMBER_PAYLOAD: dict[str, Any] = {
    "role": "scout",
    "subgoal": "Find and summarise the most relevant prior art for the objective.",
    "tools": ["web-search"],
}


def test_the_incident_payload_that_broke_a_live_compile_is_rejected_by_the_schema() -> None:
    """Consequence check, not a shape check (see the block comment above): a member payload
    carrying only ``role``/``subgoal``/``tools`` — the exact incident shape — must be REJECTED by
    the schema ``_drafter_resolved_schema`` actually produces, with a real JSON Schema validator,
    naming a missing required property. RED today for two independent reasons: ``resolved_schema``
    does not exist on ``OHMCapability`` at all on ``main`` yet, and a schema that only closes the
    OUTER object (the incident's own root cause) would still accept this payload silently — which
    is exactly what let the live compile fail with every unit test green."""
    manifest, subs = build_compiler_team(_ORG, catalog_descriptions=_CATALOG_SMALL)
    cap = _drafter_capability(subs, "draft-manifest")
    assert cap is not None, "the manifest-drafter must carry a draft-manifest capability"
    schema = cap.get("resolved_schema")
    assert schema is not None, "a surveyed catalog must produce a resolved_schema"
    member_schema = schema["properties"]["members"]["items"]

    with pytest.raises(jsonschema.exceptions.ValidationError) as exc_info:
        jsonschema.validate(_INCIDENT_MEMBER_PAYLOAD, member_schema)
    # the incident was a MISSING field, not a wrong-typed one — the rejection must name that, or a
    # schema that merely rejects the payload for some unrelated reason would pass this test while
    # still shipping the exact bug (e.g. still accepting a member with a wrong-typed 12th field
    # while remaining silent about the other 8 legitimately-missing ones).
    assert exc_info.value.validator == "required", (
        f"expected the payload to be rejected for a MISSING required property, got validator "
        f"{exc_info.value.validator!r}: {exc_info.value.message}"
    )
