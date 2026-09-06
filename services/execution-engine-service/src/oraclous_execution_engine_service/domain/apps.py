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

from oraclous_ohm.errors import OHMDagError
from oraclous_ohm.manifest import OHMManifest, OHMMember

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


def plan_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """What is going to happen, and the most it can cost — the two things a person is owed before
    they spend their own model key.

    The app read carries no team documents, because opening an app is not opening the plan behind
    it. That is right, but "no documents" is not the same as "nothing at all": someone deciding
    whether to press Run needs to know that this will search the web and write to their knowledge
    graph, and needs the ceiling the run cannot exceed. Neither is available anywhere else once a
    client stops reading the team document directly.

    So this is STRUCTURE ONLY: which steps run, in what order, what each waits on, which tools each
    may use, and the run's ceilings.

    ORDER IS THE REAL ORDER. Steps come back in execution order, grouped by ``stage`` — everything
    in one stage runs together, and the next stage waits for all of it. That order is computed by
    the SAME topological pass the runtime uses, not read off the order members happen to be written
    in. An earlier version iterated declaration order and called it execution order; it agreed with
    the runtime only by luck of how the manifest was typed, and would have shown a reader a
    confidently wrong picture of their own run.

    When the graph cannot be ordered at all — a cycle, or a dependency naming a member that does
    not exist — this does NOT raise. A team like that will fail the moment someone presses Run, and
    the honest thing is to say the order is unknown rather than either hiding the steps or inventing
    a sequence: ``ordered`` is False and every ``stage`` is None. A screen can then show the steps
    and warn, which is strictly more than it had before.

    It deliberately omits every member's ``subgoal``. A subgoal is the plan's CONTENT rather than
    its shape — the thing a shared app should not publish, and in an app someone authored it can
    carry their own words. Note the honest limit of that guarantee: it is an exclusion of the field
    that holds prose today, not proof that no author text can appear. ``role`` is free text. Before
    user-authored apps become readable by other organisations, this needs to become an allowlist of
    what may be published rather than a list of what may not.

    A missing ceiling reads as ``None``, never 0: unlimited and "zero allowed" are opposite claims,
    and a screen rendering the wrong one would mislead exactly the person this exists to inform.
    """
    team = OHMManifest.model_validate(manifest)
    by_role = {member.role: member for member in team.members}

    try:
        stages = team.execution_stages()
    except OHMDagError:
        stages = None

    steps: list[dict[str, Any]] = []
    if stages is None:
        steps = [_step(member, stage=None) for member in team.members]
    else:
        for index, roles in enumerate(stages):
            # sorted() so a stage's members come back the same way on every read — a fan-out has no
            # inherent order, and a list that reshuffles between reads reads as movement to a user.
            steps.extend(
                _step(by_role[role], stage=index) for role in sorted(roles) if role in by_role
            )

    budget = team.budget
    return {
        "steps": steps,
        "ordered": stages is not None,
        "limits": {
            "max_tokens_total": budget.max_tokens_total if budget else None,
            "max_tool_calls_total": budget.max_tool_calls_total if budget else None,
            "max_sub_runs": budget.max_sub_runs if budget else None,
        },
    }


def _step(member: OHMMember, *, stage: int | None) -> dict[str, Any]:
    """One step, as a reader sees it. Structure only — never the member's prompt."""
    return {
        "role": member.role,
        "kind": member.kind,
        "stage": stage,
        "depends_on": list(member.depends_on),
        "tools": list(member.tools),
    }


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
