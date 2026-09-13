"""#900 — ``answer_from_tool`` on ``OHMMember`` (ADR-053 decision 2).

ADR-053 rules that "a tool call is the member's answer" is declared on the member descriptor
itself, not the run's settings bundle — ``OHMMember`` gains one new, optional field::

    answer_from_tool: str | None = None

naming a tool (one of the member's own ``tools[]``) whose terminating call constitutes the
member's answer. Unset (``None``, the default) means behaviour is byte-identical to today. The
ADR's own reasoning: every other per-member behaviour already lives on the member descriptor
(``tools``, ``max_tokens``/``max_tool_calls``, ``on_exhaustion``, ``requires_valid_json``,
``outputs_schema``, ``outcome_critical``) — a saved team must be self-describing.

RED-by-design until the ``[impl]`` lands: ``OHMMember`` carries no ``answer_from_tool`` field
today, and pydantic v2 with ``extra="ignore"`` silently DROPS an unknown constructor key rather
than raising — so, mirroring #730/#834's own documented rationale, every assertion here reads the
field's VALUE after construction/round-trip, never merely "construction did not raise" (which
would pass today and prove nothing).

Whether ``answer_from_tool`` must name an entry already present in the member's own ``tools[]``
is NOT ruled by ADR-053 — the ADR's "Scope not decided here" section covers only what happens
when the loop ends without calling the named tool (existing on_exhaustion/grounding behaviour,
unchanged), and neither ``_human_requires_role`` nor any other ``OHMMember`` validator today
cross-checks any field against ``tools[]``. This file therefore pins only that the field is
independently settable — it does NOT assert or require a `tools[]`-membership validator, since
inventing one here would be deciding an ``[impl]`` question test-author has no mandate to rule on.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.manifest import OHMMember

pytestmark = pytest.mark.unit


# ── presence + optionality ───────────────────────────────────────────────────────────────────


def test_member_accepts_answer_from_tool_and_returns_it() -> None:
    m = OHMMember(
        role="drafter",
        kind="agent",
        manifest_ref="org:x/drafter@1",
        tools=["web.search"],
        answer_from_tool="web.search",
    )
    assert m.answer_from_tool == "web.search"


def test_member_answer_from_tool_defaults_to_none_when_absent() -> None:
    m = OHMMember(role="drafter", kind="agent", manifest_ref="org:x/drafter@1")
    assert m.answer_from_tool is None


def test_answer_from_tool_defaults_to_none_explicitly_pinned_not_missing() -> None:
    m = OHMMember(role="drafter", kind="agent", manifest_ref="org:x/drafter@1")
    assert m.answer_from_tool is None
    assert hasattr(m, "answer_from_tool")


# ── round-trip through model_dump / model_validate ───────────────────────────────────────────


def test_answer_from_tool_round_trips_through_model_dump_json() -> None:
    m = OHMMember(
        role="drafter",
        kind="agent",
        manifest_ref="org:x/drafter@1",
        tools=["web.search"],
        answer_from_tool="web.search",
    )
    dumped = m.model_dump(mode="json")
    assert "answer_from_tool" in dumped
    assert dumped["answer_from_tool"] == "web.search"


def test_answer_from_tool_survives_a_model_validate_round_trip() -> None:
    m = OHMMember(
        role="drafter",
        kind="agent",
        manifest_ref="org:x/drafter@1",
        tools=["web.search"],
        answer_from_tool="web.search",
    )
    reloaded = OHMMember.model_validate(m.model_dump(mode="json"))
    assert reloaded.answer_from_tool == "web.search"


# ── back-compat: a member built the OLD way is unaffected ───────────────────────────────────


def test_old_style_member_construction_still_defaults_to_none() -> None:
    # exactly how every existing call site constructs a member today — no answer_from_tool kwarg.
    m = OHMMember(
        role="drafter", kind="agent", manifest_ref="org:x/drafter@1", tools=["web.search"]
    )
    assert m.answer_from_tool is None
    dumped_excluding_none = m.model_dump(mode="json", exclude_none=True)
    assert "answer_from_tool" not in dumped_excluding_none


def test_a_plain_model_dump_includes_the_key_as_none_when_unset() -> None:
    # NOT exclude_none — the dump convention every non-compiler call site actually uses today
    # (team_draft_service.py, assemble.py, compiler_run_service.py all call bare
    # .model_dump(mode="json")). A plain dump of an old-style member WILL now carry an
    # `"answer_from_tool": null` key it never carried before — pinned here explicitly so this is
    # a documented, deliberate consequence, not a silent surprise for #900's implementer.
    m = OHMMember(role="drafter", kind="agent", manifest_ref="org:x/drafter@1")
    dumped = m.model_dump(mode="json")
    assert "answer_from_tool" in dumped
    assert dumped["answer_from_tool"] is None


# ── independence from tools[] (ADR-053 does not rule cross-field validation here) ────────────


def test_answer_from_tool_does_not_require_membership_in_tools() -> None:
    # No validator in manifest.py today cross-checks answer_from_tool against tools[], and
    # ADR-053 does not rule that one must exist. Pin the field as independently settable; do not
    # invent a requirement here.
    m = OHMMember(
        role="drafter",
        kind="agent",
        manifest_ref="org:x/drafter@1",
        tools=["some.other.tool"],
        answer_from_tool="a.tool.not.in.the.ceiling",
    )
    assert m.answer_from_tool == "a.tool.not.in.the.ceiling"
    assert m.tools == ["some.other.tool"]


def test_answer_from_tool_settable_with_no_tools_declared_at_all() -> None:
    m = OHMMember(
        role="drafter", kind="agent", manifest_ref="org:x/drafter@1", answer_from_tool="x"
    )
    assert m.answer_from_tool == "x"
    assert m.tools == []


# ── independence from the member's other declared fields ────────────────────────────────────


def test_a_member_may_declare_both_answer_from_tool_and_requires_valid_json() -> None:
    m = OHMMember(
        role="drafter",
        kind="agent",
        manifest_ref="org:x/drafter@1",
        tools=["web.search"],
        answer_from_tool="web.search",
        requires_valid_json=True,
    )
    assert m.answer_from_tool == "web.search"
    assert m.requires_valid_json is True


def test_a_member_may_declare_only_requires_valid_json_and_not_answer_from_tool() -> None:
    m = OHMMember(
        role="drafter", kind="agent", manifest_ref="org:x/drafter@1", requires_valid_json=True
    )
    assert m.requires_valid_json is True
    assert m.answer_from_tool is None  # NOT implied by requires_valid_json
