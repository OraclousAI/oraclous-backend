"""Where an app came from, and which fields its form draws (#932).

Two wire shapes the console reads, both ruled by the owner on Contract #877 (5 Sep) and both
deliberately DERIVED rather than authored:

* ``origin`` is computed from the row's ``organisation_id``. It is not a column, so a create body
  can never assert ``"platform"`` and the value can never drift from what row-level security
  already enforces.
* ``inputs`` restates what the team manifest already declares — the ``task_input.key`` plus each
  member's ``fan_out.over``. The engine already computes that exact set in ``_declared_input_keys``
  and already refuses anything outside it at GO (#714). Restating it means the console cannot draw
  a field the server would then reject.

What is NOT here, on purpose: labels, widget types, options, ordering, and which inputs the author
left changeable. That is the app-descriptor layer ADR-052 decision 3 ruled must exist and #845 owns.
A partial version shipped here is precisely what #845 exists to prevent.

RED until ``domain/apps.py`` lands; the seam is imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]

PLATFORM_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
TENANT_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _team(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "desk-research-team",
            "owner_organization_id": str(PLATFORM_ORG),
            "kind": "team",
        },
        "task_input": {
            "required": True,
            "key": "task",
            "description": "The idea, product, or decision being validated by this run.",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:desk/researcher@1",
                "subgoal": "gather evidence",
                "depends_on": [],
                "outputs_schema": {"required": ["summary"]},
            }
        ],
        "runtime": {"entrypoint": "researcher"},
    }
    manifest.update(overrides)
    return manifest


def test_an_app_owned_by_the_platform_org_reads_as_platform() -> None:
    from oraclous_execution_engine_service.domain.apps import derive_origin

    assert derive_origin(PLATFORM_ORG, platform_org_id=PLATFORM_ORG) == "platform"


def test_an_app_owned_by_a_tenant_reads_as_organisation() -> None:
    from oraclous_execution_engine_service.domain.apps import derive_origin

    assert derive_origin(TENANT_ORG, platform_org_id=PLATFORM_ORG) == "organisation"


def test_the_two_origin_values_are_the_only_ones() -> None:
    """Pinned so a third kind — a partner-published app, say — has to arrive as a deliberate
    contract change on #877 rather than by someone adding a string."""
    from oraclous_execution_engine_service.domain.apps import (
        ORIGIN_ORGANISATION,
        ORIGIN_PLATFORM,
        ORIGINS,
    )

    assert ORIGIN_PLATFORM == "platform"
    assert ORIGIN_ORGANISATION == "organisation"
    assert set(ORIGINS) == {"platform", "organisation"}


def test_the_form_draws_the_declared_task_field_with_its_description() -> None:
    """The desk's whole form is one field. The description is the manifest's own sentence, so the
    screen explains the field without the console inventing copy for it."""
    from oraclous_execution_engine_service.domain.apps import form_fields

    fields = form_fields(_team())

    assert fields == [
        {
            "key": "task",
            "required": True,
            "description": "The idea, product, or decision being validated by this run.",
        }
    ]


def test_a_fan_out_key_is_a_field_too() -> None:
    """A member that fans out over a caller-supplied list reads that list from ``inputs``, so it is
    a field the person running the app must fill. Both the JSONPath spelling and the bare key are
    accepted by the engine, so both must project to the same key."""
    from oraclous_execution_engine_service.domain.apps import form_fields

    manifest = _team()
    manifest["members"].append(
        {
            "role": "reader",
            "kind": "agent",
            "manifest_ref": "org:desk/reader@1",
            "subgoal": "read each source",
            "depends_on": ["researcher"],
            "outputs_schema": {"required": ["summary"]},
            "fan_out": {"over": "$.sources", "as": "source"},
        }
    )

    keys = [f["key"] for f in form_fields(manifest)]
    assert keys == ["task", "sources"]
    assert next(f for f in form_fields(manifest) if f["key"] == "sources")["required"] is False


def test_a_team_that_declares_no_task_input_draws_no_task_field() -> None:
    """Not every team takes a task. An app for one must not show an empty box the run would then
    refuse as an undeclared key."""
    from oraclous_execution_engine_service.domain.apps import form_fields

    manifest = _team()
    del manifest["task_input"]

    assert form_fields(manifest) == []


def test_an_optional_task_input_is_reported_optional() -> None:
    from oraclous_execution_engine_service.domain.apps import form_fields

    manifest = _team()
    manifest["task_input"]["required"] = False

    assert form_fields(manifest)[0]["required"] is False


def test_the_engine_reserved_keys_are_never_offered_as_fields() -> None:
    """``_refresh_seed`` and ``answers`` are read by the engine on every team's behalf, not supplied
    through an app's form. ``answers`` especially: it is the validation desk's documented one-off
    (#846), scheduled by ADR-052 for removal onto the descriptor layer, so the apps layer must stay
    ignorant of it rather than learning to offer it.

    The team here DECLARES both names itself — a fan-out over ``answers`` and one over
    ``_refresh_seed`` — because a fixture that never mentions them cannot tell correct exclusion
    from no exclusion logic at all. A team really can spell a member's fan-out that way, and the
    engine still reads those two keys for its own purposes, so an app form must not hand them to a
    person to fill in."""
    from oraclous_execution_engine_service.domain.apps import form_fields

    manifest = _team()
    for role, over in (("answerer", "$.answers"), ("refresher", "$._refresh_seed")):
        manifest["members"].append(
            {
                "role": role,
                "kind": "agent",
                "manifest_ref": f"org:desk/{role}@1",
                "subgoal": "read each item",
                "depends_on": ["researcher"],
                "outputs_schema": {"required": ["summary"]},
                "fan_out": {"over": over, "as": "item"},
            }
        )

    keys = [f["key"] for f in form_fields(manifest)]
    assert keys == ["task"]
