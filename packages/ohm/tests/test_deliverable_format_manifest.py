"""#730 — ``deliverable_format`` on ``OHMManifest`` and ``OHMMember`` (knowledge PR 101, §DELIV).

The owner's ruling (2026-09-04): two OPTIONAL fields, both named ``deliverable_format``, one on the
team (the form the USER receives) and one on a member (the form THAT MEMBER hands on). Accepted
values: ``markdown``/``text`` are supported today; ``pdf``/``docx``/``html`` are members of the same
accepted value set but DECLARED-AND-RESERVED — refused at definition time (packages/ohm/tests/
compiler/test_deliverable_format_validate.py owns that refusal; this file owns the plain field
contract: presence, optionality, independence, and that absence is never read as ``markdown``).

RED-by-design until the ``[impl]`` lands: neither ``OHMManifest`` nor ``OHMMember`` carries this
field today, so every construction/round-trip assertion below fails — a Pydantic v2 model with
``extra="ignore"`` silently DROPS an unknown key rather than raising, which is exactly why every
test here asserts the field's VALUE after construction/round-trip, never merely that construction
did not raise (a bare no-raise assertion would pass today and prove nothing).
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.manifest import OHMManifest, OHMMember
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_MID = str(uuid.uuid4())

#: the full accepted value set (knowledge PR 101 §DELIV) — supported + reserved together
_SUPPORTED = ("markdown", "text")
_RESERVED = ("pdf", "docx", "html")


def _team_doc(
    *,
    team_format: str | None = ...,  # type: ignore[assignment]  # ... = "field absent entirely"
    member_format: str | None = ...,  # type: ignore[assignment]
    second_member_format: str | None = ...,  # type: ignore[assignment]
) -> dict:
    researcher: dict = {
        "role": "researcher",
        "kind": "agent",
        "manifest_ref": "org:x/researcher@1",
        "depends_on": [],
    }
    if member_format is not ...:
        researcher["deliverable_format"] = member_format
    writer: dict = {
        "role": "writer",
        "kind": "agent",
        "manifest_ref": "org:x/writer@1",
        "depends_on": ["researcher"],
    }
    if second_member_format is not ...:
        writer["deliverable_format"] = second_member_format
    doc: dict = {
        "ohm_version": "1.1",
        "metadata": {
            "id": _MID,
            "name": "deliverable-team",
            "owner_organization_id": _ORG,
            "kind": "team",
        },
        "members": [researcher, writer],
        "runtime": {"entrypoint": "researcher"},
    }
    if team_format is not ...:
        doc["deliverable_format"] = team_format
    return doc


# ── presence + optionality (decision 4: absence stays absence) ──────────────────────────────


def test_manifest_deliverable_format_defaults_to_none_when_absent() -> None:
    m = OHMManifest.model_validate(_team_doc())
    assert m.deliverable_format is None


def test_member_deliverable_format_defaults_to_none_when_absent() -> None:
    member = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/r@1")
    assert member.deliverable_format is None


def test_absence_is_never_silently_read_as_markdown() -> None:
    """Reading absence as 'markdown' would silently change what every stored team produces —
    pin the negative explicitly, not just that the field is None."""
    m = OHMManifest.model_validate(_team_doc())
    researcher = m.member_by_role("researcher")
    assert researcher is not None
    assert m.deliverable_format != "markdown"
    assert m.deliverable_format is None
    assert researcher.deliverable_format != "markdown"
    assert researcher.deliverable_format is None


def test_a_manifest_with_no_deliverable_format_at_either_level_still_loads_and_runs_unchanged() -> (
    None
):
    """Back-compat (decision 4): a pre-#730 manifest loads, validates, and its execution stages
    resolve exactly as before — nothing about the loader's behaviour depends on this field."""
    manifest = load_ohm(_team_doc())
    assert manifest.deliverable_format is None
    assert manifest.member_by_role("researcher").deliverable_format is None  # type: ignore[union-attr]
    assert manifest.execution_stages() == [["researcher"], ["writer"]]


# ── the accepted value set: supported values construct + round-trip ─────────────────────────


@pytest.mark.parametrize("value", _SUPPORTED)
def test_manifest_accepts_a_supported_value_and_round_trips_it(value: str) -> None:
    m = OHMManifest.model_validate(_team_doc(team_format=value))
    # value pinned post-construction — never just "did not raise"
    assert m.deliverable_format == value


@pytest.mark.parametrize("value", _SUPPORTED)
def test_member_accepts_a_supported_value_and_round_trips_it(value: str) -> None:
    m = OHMManifest.model_validate(_team_doc(member_format=value))
    researcher = m.member_by_role("researcher")
    assert researcher is not None
    assert researcher.deliverable_format == value


@pytest.mark.parametrize("value", _SUPPORTED)
def test_load_ohm_round_trips_a_supported_value_at_both_levels(value: str) -> None:
    manifest = load_ohm(_team_doc(team_format=value, member_format=value))
    assert manifest.deliverable_format == value
    researcher = manifest.member_by_role("researcher")
    assert researcher is not None
    assert researcher.deliverable_format == value


@pytest.mark.parametrize("value", _RESERVED)
def test_a_reserved_value_still_constructs_at_the_plain_schema_level(value: str) -> None:
    """The ruling: pdf/docx/html are members of the ACCEPTED VALUE SET — the refusal is a
    definition-time GATE (validate_draft), not a schema-level rejection. A hand-built manifest
    carrying one still constructs; test_deliverable_format_validate.py owns the refusal."""
    m = OHMManifest.model_validate(_team_doc(team_format=value))
    assert m.deliverable_format == value


# ── decision 1: both levels exist, and they are independent ─────────────────────────────────


def test_team_and_member_level_values_coexist_without_interfering() -> None:
    m = OHMManifest.model_validate(_team_doc(team_format="markdown", member_format="text"))
    researcher = m.member_by_role("researcher")
    assert researcher is not None
    assert m.deliverable_format == "markdown"
    assert researcher.deliverable_format == "text"  # the OTHER value — neither overwrote the other


def test_team_and_member_level_values_can_differ_the_other_way() -> None:
    m = OHMManifest.model_validate(_team_doc(team_format="text", member_format="markdown"))
    researcher = m.member_by_role("researcher")
    assert researcher is not None
    assert m.deliverable_format == "text"
    assert researcher.deliverable_format == "markdown"


def test_a_member_level_value_with_no_team_level_value_does_not_set_the_team() -> None:
    """A member's declared hand-off form never propagates up to the team's declared form
    (decision 2's other half — the team's form is stated, never inferred from a member)."""
    m = OHMManifest.model_validate(_team_doc(member_format="markdown"))
    researcher = m.member_by_role("researcher")
    assert researcher is not None
    assert researcher.deliverable_format == "markdown"
    assert m.deliverable_format is None  # NOT inferred from the member that declared one


def test_a_team_level_value_with_no_member_level_value_does_not_set_the_member() -> None:
    m = OHMManifest.model_validate(_team_doc(team_format="markdown"))
    researcher = m.member_by_role("researcher")
    assert researcher is not None
    assert m.deliverable_format == "markdown"
    assert researcher.deliverable_format is None  # NOT inherited from the team


def test_two_members_may_carry_two_different_hand_off_forms() -> None:
    m = OHMManifest.model_validate(_team_doc(member_format="markdown", second_member_format="text"))
    researcher = m.member_by_role("researcher")
    writer = m.member_by_role("writer")
    assert researcher is not None and writer is not None
    assert researcher.deliverable_format == "markdown"
    assert writer.deliverable_format == "text"


# ── decision 2: adding/removing/reordering members must not change the team's declared form ─


def test_adding_a_member_to_the_dict_before_validation_does_not_change_the_teams_format() -> None:
    """A cruder version of decision 2, at the plain-manifest layer: constructing the SAME team
    doc with an extra trailing member present from the start still yields the identical team-level
    value — the field is read from its own key, never derived from iterating members[]."""
    without_second = OHMManifest.model_validate(_team_doc(team_format="markdown"))
    with_second = OHMManifest.model_validate(
        _team_doc(team_format="markdown", second_member_format="text")
    )
    assert without_second.deliverable_format == "markdown"
    assert with_second.deliverable_format == "markdown"  # unchanged by the extra member's own value


def test_reordering_members_does_not_change_the_teams_declared_format() -> None:
    doc = _team_doc(team_format="markdown")
    reordered = dict(doc)
    reordered["members"] = list(reversed(doc["members"]))
    reordered["runtime"] = {"entrypoint": "researcher"}
    m = OHMManifest.model_validate(reordered)
    assert m.deliverable_format == "markdown"


# ── decision 5: independence from requires_valid_json (#853) ────────────────────────────────


def test_a_member_may_declare_both_requires_valid_json_and_deliverable_format() -> None:
    member = OHMMember(
        role="r",
        kind="agent",
        manifest_ref="org:x/r@1",
        requires_valid_json=True,
        deliverable_format="markdown",
    )
    assert member.requires_valid_json is True
    assert member.deliverable_format == "markdown"


def test_a_member_may_declare_only_requires_valid_json() -> None:
    member = OHMMember(role="r", kind="agent", manifest_ref="org:x/r@1", requires_valid_json=True)
    assert member.requires_valid_json is True
    assert member.deliverable_format is None  # NOT implied by requires_valid_json


def test_a_member_may_declare_only_deliverable_format() -> None:
    member = OHMMember(role="r", kind="agent", manifest_ref="org:x/r@1", deliverable_format="text")
    assert member.deliverable_format == "text"
    # requires_valid_json is NOT implied by deliverable_format — the #853 default stands
    assert member.requires_valid_json is False


def test_a_member_may_declare_neither() -> None:
    member = OHMMember(role="r", kind="agent", manifest_ref="org:x/r@1")
    assert member.requires_valid_json is False
    assert member.deliverable_format is None
