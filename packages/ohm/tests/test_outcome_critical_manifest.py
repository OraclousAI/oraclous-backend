"""#834 — ``outcome_critical`` on ``OHMMember`` (manifest-level flag, ruling §A).

A member marked ``outcome_critical: true`` is one whose output IS the run's actual deliverable —
declared, not inferred (#749's own wording). This file owns the plain field contract only: it is
accepted, defaults ``False`` (every manifest written before #834 is unaffected), round-trips through
``model_dump``, and — the fail-closed load-time cross-check (ruling §A.3) — a member marked critical
with no non-empty ``outputs_schema.required`` gives the run-verdict rule nothing to check, so
``load_ohm`` rejects it rather than silently accepting a flag it can never honour.

The orchestrator verdict rule itself (a critical member's ``partial`` with an empty declared key
fails the run) is pinned in ``test_orchestrate_outcome_critical.py``, not here. The compiler draft
path's F-DRAFT-INVALID surfacing of this SAME load-time refusal is pinned in
``compiler/test_outcome_critical_validate.py``, not here.

RED-by-design until the ``[impl]`` lands: ``OHMMember`` carries no ``outcome_critical`` field today,
and Pydantic v2 with ``extra="ignore"`` silently DROPS an unknown constructor key rather than
raising — so, mirroring #730's own documented rationale, every assertion here reads the field's
VALUE after construction/round-trip, never merely "construction did not raise" (which would pass
today and prove nothing).
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.manifest import OHMManifest, OHMMember

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_MID = str(uuid.uuid4())


def _team_doc(members: list[dict]) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": _MID,
            "name": "outcome-critical-team",
            "owner_organization_id": _ORG,
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


# ── presence + optionality ───────────────────────────────────────────────────────────────────


def test_member_accepts_outcome_critical_true() -> None:
    m = OHMMember(
        role="reviewer",
        kind="agent",
        manifest_ref="org:x/reviewer@1",
        outcome_critical=True,
        outputs_schema={"required": ["members"]},
    )
    assert m.outcome_critical is True


def test_outcome_critical_defaults_to_false_back_compat() -> None:
    # every manifest stored before #834 carries no `outcome_critical` key at all
    m = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/r@1")
    assert m.outcome_critical is False


def test_outcome_critical_defaults_to_false_explicitly_pinned_not_none() -> None:
    # a bare `is not True` would also pass if the default were silently None — pin the exact value
    m = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/r@1")
    assert m.outcome_critical is False
    assert m.outcome_critical is not None


def test_a_stored_manifest_with_no_outcome_critical_key_loads_unaffected() -> None:
    from oraclous_ohm.parse import load_ohm

    doc = _team_doc(
        [
            {"role": "researcher", "kind": "agent", "manifest_ref": "org:x/r@1", "depends_on": []},
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
            },
        ]
    )
    manifest = load_ohm(doc)
    researcher = manifest.member_by_role("researcher")
    writer = manifest.member_by_role("writer")
    assert researcher is not None and writer is not None
    assert researcher.outcome_critical is False
    assert writer.outcome_critical is False


# ── round-trip through model_dump (#834 commit 1: "pin the default and the round-trip") ───────


def test_outcome_critical_round_trips_true_through_model_dump() -> None:
    m = OHMMember(
        role="reviewer",
        kind="agent",
        manifest_ref="org:x/reviewer@1",
        outcome_critical=True,
        outputs_schema={"required": ["members"]},
    )
    dumped = m.model_dump()
    assert "outcome_critical" in dumped
    assert dumped["outcome_critical"] is True


def test_outcome_critical_round_trips_false_through_model_dump() -> None:
    m = OHMMember(role="researcher", kind="agent", manifest_ref="org:x/r@1")
    dumped = m.model_dump()
    assert "outcome_critical" in dumped
    assert dumped["outcome_critical"] is False


def test_outcome_critical_survives_a_full_manifest_round_trip() -> None:
    from oraclous_ohm.parse import load_ohm

    doc = _team_doc(
        [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:x/reviewer@1",
                "outcome_critical": True,
                "outputs_schema": {"required": ["members"]},
            }
        ]
    )
    manifest = load_ohm(doc)
    reloaded = OHMManifest.model_validate(manifest.model_dump())
    reviewer = reloaded.member_by_role("reviewer")
    assert reviewer is not None
    assert reviewer.outcome_critical is True


# ── ruling §A.3: outcome_critical=true with no declared required output is rejected at load ───


def test_outcome_critical_with_no_outputs_schema_is_rejected_at_load() -> None:
    from oraclous_ohm.errors import OHMSchemaError
    from oraclous_ohm.parse import load_ohm

    doc = _team_doc(
        [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:x/reviewer@1",
                "outcome_critical": True,
                # no outputs_schema at all — the rule has nothing to check
            }
        ]
    )
    with pytest.raises(OHMSchemaError):
        load_ohm(doc)


def test_outcome_critical_with_empty_required_list_is_rejected_at_load() -> None:
    from oraclous_ohm.errors import OHMSchemaError
    from oraclous_ohm.parse import load_ohm

    doc = _team_doc(
        [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:x/reviewer@1",
                "outcome_critical": True,
                "outputs_schema": {"required": []},  # declared, but empty — still nothing to check
            }
        ]
    )
    with pytest.raises(OHMSchemaError):
        load_ohm(doc)


def test_outcome_critical_with_outputs_schema_but_no_required_key_is_rejected_at_load() -> None:
    from oraclous_ohm.errors import OHMSchemaError
    from oraclous_ohm.parse import load_ohm

    doc = _team_doc(
        [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:x/reviewer@1",
                "outcome_critical": True,
                "outputs_schema": {"type": "object"},  # a schema, but no `required` at all
            }
        ]
    )
    with pytest.raises(OHMSchemaError):
        load_ohm(doc)


def test_outcome_critical_with_a_non_empty_required_list_loads_fine() -> None:
    from oraclous_ohm.parse import load_ohm

    doc = _team_doc(
        [
            {
                "role": "reviewer",
                "kind": "agent",
                "manifest_ref": "org:x/reviewer@1",
                "outcome_critical": True,
                "outputs_schema": {"required": ["members"]},
            }
        ]
    )
    manifest = load_ohm(doc)
    reviewer = manifest.member_by_role("reviewer")
    assert reviewer is not None
    assert reviewer.outcome_critical is True


def test_a_non_critical_member_with_no_outputs_schema_is_untouched() -> None:
    # the load-time refusal is scoped to outcome_critical members only — a non-critical member with
    # no declared output contract is exactly today's (permissive) behaviour.
    from oraclous_ohm.parse import load_ohm

    doc = _team_doc(
        [{"role": "researcher", "kind": "agent", "manifest_ref": "org:x/r@1", "depends_on": []}]
    )
    manifest = load_ohm(doc)  # must not raise
    researcher = manifest.member_by_role("researcher")
    assert researcher is not None
    assert researcher.outcome_critical is False
