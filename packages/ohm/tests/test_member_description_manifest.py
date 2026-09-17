"""#1085 — ``description`` on ``OHMMember`` (CTO ruling on the app-read plan contract).

A member's own one-sentence, author-declared description is what an app read shows on the step for
that role (``execution-engine-service``'s ``plan_summary``). Today ``OHMMember`` is
``extra="ignore"`` (packages/ohm/src/oraclous_ohm/manifest.py, ``class OHMMember``), so a
``description`` key written into a manifest is silently DROPPED at validation — there is no
``OHMMember.description`` attribute at all. That is the RED: every assertion below reads the value
back off a constructed/round-tripped model, so a bare "did not raise" can never pass by accident.

Deliberately narrow (unlike #730's ``deliverable_format``, which lives at both team and member
level): the ruling only adds a field to the member. Blank/whitespace trimming and "never derived
from subgoal" are the plan's job (``services/execution-engine-service/tests/unit/test_app_plan.py``)
and are not re-asserted here — this file owns only the schema-level field: presence, optionality,
and that it survives ``OHMManifest.model_validate(...)`` -> ``model_dump()``.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.manifest import OHMManifest, OHMMember

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_MID = str(uuid.uuid4())


def _team_doc(*, description: str | None = ...) -> dict:  # type: ignore[assignment]
    """A minimal, valid two-member team. ``...`` means "key absent entirely"."""
    researcher: dict = {
        "role": "researcher",
        "kind": "agent",
        "manifest_ref": "org:x/researcher@1",
        "depends_on": [],
    }
    if description is not ...:
        researcher["description"] = description
    writer: dict = {
        "role": "writer",
        "kind": "agent",
        "manifest_ref": "org:x/writer@1",
        "depends_on": ["researcher"],
    }
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": _MID,
            "name": "description-team",
            "owner_organization_id": _ORG,
            "kind": "team",
        },
        "members": [researcher, writer],
        "runtime": {"entrypoint": "researcher"},
    }


# ── the plain field: construction, presence, optionality ────────────────────────────────────


def test_member_accepts_an_optional_description() -> None:
    member = OHMMember(
        role="researcher",
        kind="agent",
        manifest_ref="org:x/researcher@1",
        description="Searches the web for competitor signals.",
    )
    # value pinned post-construction — never merely "did not raise"
    assert member.description == "Searches the web for competitor signals."


def test_member_description_defaults_to_none_when_absent() -> None:
    member = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/r@1")
    assert member.description is None


# ── it survives OHMManifest.model_validate(...) -> model_dump() ─────────────────────────────


def test_manifest_validate_keeps_a_members_declared_description() -> None:
    """The ruling's own words: 'Today OHMMember is extra="ignore", so a description written into a
    manifest is silently dropped on validation; that is why the field must be declared.'"""
    manifest = OHMManifest.model_validate(
        _team_doc(description="Searches the web for competitor signals.")
    )
    researcher = manifest.member_by_role("researcher")
    assert researcher is not None
    assert researcher.description == "Searches the web for competitor signals."


def test_model_dump_round_trips_the_declared_description() -> None:
    manifest = OHMManifest.model_validate(_team_doc(description="Writes one decision brief."))
    dumped = manifest.model_dump()
    researcher_dump = next(m for m in dumped["members"] if m["role"] == "researcher")
    assert researcher_dump["description"] == "Writes one decision brief."


def test_a_member_with_no_declared_description_validates_and_reads_none() -> None:
    """A member with no description is a perfectly valid member — absence is not an error."""
    manifest = OHMManifest.model_validate(_team_doc())
    researcher = manifest.member_by_role("researcher")
    writer = manifest.member_by_role("writer")
    assert researcher is not None and writer is not None
    assert researcher.description is None
    assert writer.description is None


def test_model_dump_carries_the_key_as_none_when_no_description_was_declared() -> None:
    """The dumped shape carries the key with a None value, not an absent key — a client can rely
    on ``description`` always being present on a dumped member."""
    manifest = OHMManifest.model_validate(_team_doc())
    dumped = manifest.model_dump()
    researcher_dump = next(m for m in dumped["members"] if m["role"] == "researcher")
    assert "description" in researcher_dump
    assert researcher_dump["description"] is None


def test_two_members_may_carry_two_different_descriptions() -> None:
    doc = _team_doc(description="Searches the web.")
    doc["members"][1]["description"] = "Writes the brief."
    manifest = OHMManifest.model_validate(doc)
    researcher = manifest.member_by_role("researcher")
    writer = manifest.member_by_role("writer")
    assert researcher is not None and writer is not None
    assert researcher.description == "Searches the web."
    assert writer.description == "Writes the brief."
