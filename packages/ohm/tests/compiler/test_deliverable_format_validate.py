"""#730 — ``deliverable_format`` is refused at DEFINITION time (knowledge PR 101, §DELIV, decision
3 of 5).

``validate_draft`` is the shared definition-time gate (ADR-047/#593, "one validator, two on-ramps")
every draft write and every compiled team runs through. The ruling: ``markdown``/``text`` are
SUPPORTED; ``pdf``/``docx``/``html`` are members of the same accepted value set but RESERVED —
refused here, never at run time. The refusal must carry a DIFFERENT coded reason than a genuinely
UNKNOWN value ("not supported yet" vs "unrecognised") — a misspelling and a not-yet-built format are
different problems, and conflating them sends the user hunting for a typo that is not there.

Flag codes chosen for this gate (none existed before #730 — picked to fit the ``F-*`` family
alongside ``F-CAPABILITY-MISSING``/``F-SUBSTRATE-FILE``/``F-NO-OUTPUT-CONTRACT``):

  * ``F-DELIVERABLE-FORMAT-RESERVED`` — the value IS a member of the accepted value set (pdf/docx/
    html) but not supported yet.
  * ``F-DELIVERABLE-FORMAT-UNKNOWN``  — the value is NOT a member of the accepted value set at all
    (a typo / an invented format) — a genuinely different problem, a genuinely different code.

Both apply at TEAM level (``member_role=""``) and at MEMBER level (``member_role=<role>``) — the
ruling names both surfaces identically.

RED-by-design: ``validate_draft`` has no opinion on ``deliverable_format`` today, so every draft
below that SHOULD block currently reports ``would_block: False`` — these tests fail on that
boolean until the ``[impl]`` lands. ``test_deliverable_format_manifest.py`` (one directory up) owns
the plain field contract (presence/optionality/independence); this file owns only the refusal.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.refine import AddMember, RemoveMember, apply_refine
from oraclous_ohm.compiler.validate import validate_draft
from oraclous_ohm.import_ import assemble_and_report
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG: list[str] = []

_SUPPORTED = ("markdown", "text")
_RESERVED = ("pdf", "docx", "html")


def _draft(*, team_format: str | None = None, member_format: str | None = None) -> dict:
    # #697/#718 defaults every fixture in this package carries so a draft that SHOULD pass is
    # never blocked for an unrelated reason.
    member: dict = {
        "role": "researcher",
        "kind": "agent",
        "manifest_ref": "org:x/r@1",
        "outputs_schema": {"required": ["summary"]},
    }
    if member_format is not None:
        member["deliverable_format"] = member_format
    draft: dict = {"members": [member]}
    if team_format is not None:
        draft["deliverable_format"] = team_format
    return draft


# ── a supported value passes cleanly at both levels ──────────────────────────────────────────


@pytest.mark.parametrize("value", _SUPPORTED)
def test_a_supported_team_level_format_passes(value: str) -> None:
    v = validate_draft(_draft(team_format=value), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False


@pytest.mark.parametrize("value", _SUPPORTED)
def test_a_supported_member_level_format_passes(value: str) -> None:
    v = validate_draft(_draft(member_format=value), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False


def test_a_draft_declaring_neither_still_passes() -> None:
    """Back-compat: a draft with no deliverable_format anywhere validates exactly as before."""
    v = validate_draft(_draft(), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False


# ── a reserved value is refused — "not supported yet", never "unrecognised" ─────────────────


@pytest.mark.parametrize("value", _RESERVED)
def test_a_reserved_team_level_format_is_refused_as_not_supported_yet(value: str) -> None:
    v = validate_draft(_draft(team_format=value), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-DELIVERABLE-FORMAT-RESERVED" in b for b in v["blocking"])
    reserved = next(b for b in v["blocking"] if "F-DELIVERABLE-FORMAT-RESERVED" in b)
    # the reason names "not supported yet" (or equivalent), never sends the user hunting for a typo
    assert "not supported" in reserved.lower() or "reserved" in reserved.lower()
    assert "unrecognised" not in reserved.lower() and "unrecognized" not in reserved.lower()


@pytest.mark.parametrize("value", _RESERVED)
def test_a_reserved_member_level_format_is_refused_and_names_the_member(value: str) -> None:
    v = validate_draft(_draft(member_format=value), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True
    reserved = [b for b in v["blocking"] if "F-DELIVERABLE-FORMAT-RESERVED" in b]
    assert reserved, v["blocking"]
    assert any("researcher" in b for b in reserved)  # the member role rides in the message


# ── a genuinely unknown value is refused too — but with a DIFFERENT coded reason ────────────


def test_an_unknown_team_level_format_is_refused_as_unrecognised() -> None:
    v = validate_draft(_draft(team_format="banana"), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-DELIVERABLE-FORMAT-UNKNOWN" in b for b in v["blocking"])
    unknown = next(b for b in v["blocking"] if "F-DELIVERABLE-FORMAT-UNKNOWN" in b)
    assert "not supported" not in unknown.lower()  # never conflated with the reserved reason


def test_an_unknown_member_level_format_is_refused_as_unrecognised() -> None:
    v = validate_draft(_draft(member_format="banana"), _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-DELIVERABLE-FORMAT-UNKNOWN" in b for b in v["blocking"])


def test_reserved_and_unknown_carry_genuinely_different_codes() -> None:
    """The load-bearing assertion for decision 3: the two refusals are not the same code wearing
    two different messages — pdf and banana must never collapse onto one reason."""
    reserved = validate_draft(_draft(team_format="pdf"), _CATALOG, owner_organization_id=_ORG)
    unknown = validate_draft(_draft(team_format="banana"), _CATALOG, owner_organization_id=_ORG)
    reserved_codes = {b.split(":", 1)[0] for b in reserved["blocking"]}
    unknown_codes = {b.split(":", 1)[0] for b in unknown["blocking"]}
    assert "F-DELIVERABLE-FORMAT-RESERVED" in reserved_codes
    assert "F-DELIVERABLE-FORMAT-UNKNOWN" in unknown_codes
    assert reserved_codes.isdisjoint(unknown_codes)


# ── decision 2: adding a member does not change the team's declared form ────────────────────


def _members(deliverable_format: str | None = None) -> list[OHMMember]:
    return [
        OHMMember(
            role="researcher",
            kind="agent",
            manifest_ref="org:x/r@1",
            deliverable_format=deliverable_format,
        ),
    ]


def test_assemble_keeps_the_declared_team_level_deliverable_format() -> None:
    """Mirrors the #714 task_input precedent: assemble_and_report REBUILDS the manifest from
    members alone, so the team-level field is dropped at the peel unless it is threaded through."""
    result = assemble_and_report(
        "compiled-team",
        _members(),
        owner_organization_id=_ORG,
        shape="compiled",
        deliverable_format="markdown",
    )
    assert result.manifest is not None
    assert result.manifest.deliverable_format == "markdown"


def test_assemble_without_a_deliverable_format_is_unchanged() -> None:
    result = assemble_and_report(
        "compiled-team", _members(), owner_organization_id=_ORG, shape="compiled"
    )
    assert result.manifest is not None
    assert result.manifest.deliverable_format is None


def _team_manifest(*, team_format: str | None, member_format: str | None) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=_members(member_format),
        runtime=OHMRuntime(entrypoint="researcher"),
        deliverable_format=team_format,
    )


def test_add_member_refine_does_not_change_the_teams_declared_format() -> None:
    manifest = _team_manifest(team_format="markdown", member_format="text")
    result = apply_refine(
        manifest,
        AddMember(role="fact-checker", outputs_schema={"required": ["summary"]}),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert result.manifest is not None, result.report.blocking
    assert result.manifest.deliverable_format == "markdown"  # unchanged by the addition
    original = result.manifest.member_by_role("researcher")
    assert original is not None and original.deliverable_format == "text"  # preserve-the-rest
    new_member = result.manifest.member_by_role("fact-checker")
    assert new_member is not None
    assert new_member.deliverable_format is None  # never inherited from the team


def test_remove_member_refine_does_not_change_the_teams_declared_format() -> None:
    manifest = _team_manifest(team_format="markdown", member_format="text")
    # give the manifest a second member so the first can be removed without hitting the
    # entrypoint guard (#750's F-REFINE-ENTRYPOINT)
    second = OHMMember(
        role="writer",
        kind="agent",
        manifest_ref="org:x/w@1",
        depends_on=["researcher"],
        deliverable_format="pdf",  # deliberately reserved-looking, but removal never validates it
    )
    manifest = manifest.model_copy(update={"members": [*manifest.members, second]})
    result = apply_refine(
        manifest,
        RemoveMember(role="writer"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert result.manifest is not None, result.report.blocking
    # the last member removed doesn't decide the team's declared form
    assert result.manifest.deliverable_format == "markdown"
    assert result.manifest.member_by_role("writer") is None
    remaining = result.manifest.member_by_role("researcher")
    assert remaining is not None and remaining.deliverable_format == "text"


def test_a_member_level_value_never_propagates_up_through_a_refine() -> None:
    manifest = _team_manifest(team_format=None, member_format="markdown")
    result = apply_refine(
        manifest,
        AddMember(role="fact-checker", outputs_schema={"required": ["summary"]}),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert result.manifest is not None, result.report.blocking
    assert result.manifest.deliverable_format is None  # the member's value never became the team's


# ── decision 5: independence from requires_valid_json (#853) ────────────────────────────────


def test_a_reserved_deliverable_format_blocks_regardless_of_requires_valid_json() -> None:
    """The two gates are independent — requires_valid_json must never shield deliverable_format's
    own refusal, and vice versa."""
    draft = _draft(member_format="pdf")
    draft["members"][0]["requires_valid_json"] = True
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is True
    assert any("F-DELIVERABLE-FORMAT-RESERVED" in b for b in v["blocking"])


def test_a_supported_deliverable_format_and_requires_valid_json_coexist_cleanly() -> None:
    draft = _draft(member_format="markdown")
    draft["members"][0]["requires_valid_json"] = True
    v = validate_draft(draft, _CATALOG, owner_organization_id=_ORG)
    assert v["would_block"] is False
