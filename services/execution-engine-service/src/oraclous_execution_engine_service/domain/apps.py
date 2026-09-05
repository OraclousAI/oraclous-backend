"""The app domain vocabulary (domain layer, #932): where an app came from, what its form asks for,
and how a caller's own model is bound onto it before a run.

Three small pieces, each deliberately thin, because each is a place where inventing more than the
platform already knows would be a mistake:

* ``derive_origin`` computes the platform-versus-organisation distinction rather than storing it;
* ``form_fields`` restates what the team manifest already declares rather than authoring a form;
* ``bind_run_documents`` reuses the shared model-binding validator rather than growing a second
  error vocabulary.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from oraclous_ohm.manifest import OHMManifest

from oraclous_execution_engine_service.services.compiler_run_service import (
    validate_model_bindings,
)
from oraclous_execution_engine_service.services.team_run_service import _declared_input_keys

#: An app Oraclous provides, seeded into the platform organisation and readable by everyone.
ORIGIN_PLATFORM = "platform"
#: An app made inside the organisation that is reading it.
ORIGIN_ORGANISATION = "organisation"
#: The complete vocabulary. A third kind — a partner-published app, say — is a contract change on
#: #877, not a new string somebody adds here.
ORIGINS: tuple[str, ...] = (ORIGIN_PLATFORM, ORIGIN_ORGANISATION)


def derive_origin(organisation_id: uuid.UUID, *, platform_org_id: uuid.UUID) -> str:
    """Where an app came from, computed from its owner.

    DERIVED, never stored (ruled by the owner on #877, 5 Sep). There is no column to write, so a
    create request cannot assert ``"platform"``, and the value cannot drift from what the row-level
    security policy already enforces. A boolean was rejected because it could not grow a third kind
    without a breaking change.
    """
    return ORIGIN_PLATFORM if organisation_id == platform_org_id else ORIGIN_ORGANISATION


def form_fields(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The controls an app's form draws, read off what the team already declares.

    This is a RESTATEMENT, not an authored form. The keys are exactly ``_declared_input_keys`` —
    the ``task_input.key`` plus each member's ``fan_out.over`` — which is the same set the engine
    refuses to exceed at GO (#714). Restating it is what stops the console offering a field the
    server would then reject.

    Engine-reserved keys are never offered even when a team happens to declare one: the engine reads
    those on every team's behalf, so they are not a person's to fill in. ``answers`` matters most —
    it is the validation desk's documented one-off (#846) which ADR-052 schedules for removal, and
    the apps layer must stay ignorant of it rather than learning to offer it.

    Deliberately carries NO labels, widget types, options, ordering, or any notion of which inputs
    an author left changeable. That is the app-descriptor layer ADR-052 decision 3 ruled must exist
    and #845 owns; a partial version here is what that issue exists to prevent.
    """
    from oraclous_execution_engine_service.services.team_run_service import _ENGINE_RESERVED_KEYS

    team = OHMManifest.model_validate(manifest)
    declared = _declared_input_keys(team)

    fields: list[dict[str, Any]] = []
    task_input = team.task_input
    if task_input is not None and task_input.key not in _ENGINE_RESERVED_KEYS:
        fields.append(
            {
                "key": task_input.key,
                "required": bool(task_input.required),
                "description": task_input.description,
            }
        )

    task_key = task_input.key if task_input is not None else None
    for key in sorted(declared - _ENGINE_RESERVED_KEYS - {task_key}):
        # A fan-out key is a list the caller supplies; the manifest states no requiredness for it,
        # and a member that fans out over nothing simply does no work — so it is optional.
        fields.append({"key": key, "required": False, "description": None})
    return fields


def bind_run_documents(
    manifest: dict[str, Any],
    sub_harnesses: dict[str, dict[str, Any]],
    *,
    models: list[dict[str, Any]] | None,
    organisation_id: uuid.UUID,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Copy an app's frozen documents and bind the CALLER's model and organisation onto the copy.

    The owner ruled that a default app runs on the caller's key, never an Oraclous-owned one. This
    enforces it rather than asking for it: freezing stripped every credential and the seed never had
    one, so a stored app has nothing to run on and the only key in play is the one supplied here.
    The engine still never holds the key itself — only a credential id the caller owns (ADR-008).

    Both binding sites matter. The team-level entry is what a reader of the app sees and what the
    intake read-back binds against (#927); each member's entry is what that member actually runs on.

    The organisation rewrite is load-bearing, not cosmetic: nothing in ``TeamRunService.create``
    validates ``metadata.owner_organization_id``, it is simply propagated into every synthesized
    member document — so without this a platform app would carry Oraclous's organisation into a
    tenant's run.

    Copies rather than edits: the stored row is shared by every organisation that can see the app,
    and binding onto it in place would put one tenant's credential in another tenant's read.
    """
    bound = validate_model_bindings(models, who="an app run")

    run_manifest = copy.deepcopy(manifest)
    run_manifest["models"] = bound
    metadata = run_manifest.setdefault("metadata", {})
    metadata["owner_organization_id"] = str(organisation_id)

    run_subs: dict[str, dict[str, Any]] = {}
    for role, document in copy.deepcopy(sub_harnesses).items():
        document["models"] = bound
        run_subs[role] = document
    return run_manifest, run_subs
