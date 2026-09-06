"""Turning a finished run into an app (#938) — docker-required.

#932 built every read path an app has and deliberately shipped no create one: the owner's ruling
was that turning a team into an app needed designing first. This is that path, ruled on 6 Sep 2026:
an app is made from ONE run that SUCCEEDED, it freezes the documents that run actually executed, and
it carries a form a model drafted from the person's own request and the person then edited.

Real Postgres and the real repositories throughout. Two of these are about what the service
REFUSES, and a fake repository would agree with whatever the service happens to do.

``team_runs`` is a recording stub in the one test that starts a run. That is not a fake standing in
for a service under test — it is how the assertion reads what ``run`` HANDS to the run path, which
is the only thing this layer decides. That the handed request is then accepted end to end is proven
against the deployed stack in ``tests/e2e/test_app_from_run_gateway_e2e.py``, not here.

RED until ``AppService.create_from_run`` lands; every seam is imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType

pytestmark = [pytest.mark.integration]

PLATFORM_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")

FIELDS: list[dict[str, Any]] = [
    {
        "name": "Competitor",
        "hint": "The company this brief is about.",
        "type": "short_text",
        "example": "Acme Cloud",
        "required": True,
    },
    {
        "name": "Focus",
        "hint": "What angle to take.",
        "type": "short_text",
        "example": "their pricing move",
        "required": True,
    },
]


def _principal(org: uuid.UUID | None) -> Principal:
    return Principal(principal_id=USER_A, principal_type=PrincipalType.USER, organisation_id=org)


def _team(org: uuid.UUID) -> dict[str, Any]:
    """A team carrying an author's model credential, so the freeze has something real to strip."""
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "competitor-brief",
            "owner_organization_id": str(org),
            "kind": "team",
        },
        "task_input": {"required": True, "key": "task", "description": "The competitor and angle."},
        "models": [
            {
                "binding": "default",
                "provider": "openrouter",
                "model": "openai/gpt-4o",
                "config": {"credential_id": str(uuid.uuid4()), "temperature": 0.2},
            }
        ],
        "members": [
            {
                "role": "scout",
                "kind": "agent",
                "manifest_ref": "org:brief/scout@1",
                "subgoal": "gather evidence",
                "depends_on": [],
                "outputs_schema": {"required": ["summary"]},
            }
        ],
        "runtime": {"entrypoint": "scout"},
    }


class _RecordingRuns:
    """Captures the one call ``AppService.run`` makes, so the fold can be read off it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, principal: Principal, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return object()


@pytest.fixture
async def wired(engine_dsns) -> AsyncIterator[Any]:  # noqa: ANN001
    """The real app service on the real repositories, plus a real team-run repository so the tests
    can put a genuine run row in front of it."""
    from oraclous_execution_engine_service.repositories.app_repository import AppRepository
    from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
    from oraclous_execution_engine_service.services.app_service import AppService

    _owner_async_dsn, app_async_dsn = engine_dsns
    apps = AppRepository(app_async_dsn, platform_org_id=PLATFORM_ORG)
    runs = TeamRunRepository(app_async_dsn)
    recorder = _RecordingRuns()
    try:
        service = AppService(
            apps=apps,
            team_runs=recorder,  # type: ignore[arg-type]
            team_run_repository=runs,
            platform_org_id=PLATFORM_ORG,
        )
        yield service, runs, recorder
    finally:
        await apps.close()
        await runs.close()


async def _finished_run(runs: Any, org: uuid.UUID, *, state: str = "SUCCEEDED") -> Any:
    """A real run row in ``state``, carrying the documents it executed."""
    from oraclous_execution_engine_service.core.rls import org_scope

    with org_scope(org):
        row = await runs.create(
            organisation_id=org,
            user_id=USER_A,
            manifest=_team(org),
            sub_harnesses={"scout": {"models": [{"binding": "default", "config": {}}]}},
            gate_decisions={},
            inputs={"task": "Write a competitor brief on Acme Cloud, focused on pricing."},
        )
        if state != "QUEUED":
            await runs.transition(row.id, org, new_state=state, allowed_from=frozenset({"QUEUED"}))
        return await runs.get(row.id, org)


# ── the ordinary path ────────────────────────────────────────────────────────


async def test_a_succeeded_run_becomes_an_app_owned_by_the_caller(wired: Any) -> None:
    """The whole point of the issue. The app belongs to the caller's organisation, names the run it
    came from, and starts at version one — a converted app is pinned, not tracking anything."""
    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)

    detail, created = await service.create_from_run(
        _principal(ORG_A),
        team_run_id=run.id,
        name="Competitor Brief",
        description="One page on a named competitor.",
        fields=FIELDS,
    )

    assert created is True
    assert detail["origin"] == "organisation"
    assert detail["source_team_run_id"] == run.id
    assert detail["pinned_version"] == 1
    assert detail["name"] == "Competitor Brief"
    assert [f["name"] for f in detail["form"]] == ["Competitor", "Focus"]


async def test_the_handle_is_generated_from_the_name(wired: Any) -> None:
    """Nobody types it. The console links to a readable handle instead of a uuid, exactly as it now
    does for the Oraclous-provided app."""
    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)

    detail, _ = await service.create_from_run(
        _principal(ORG_A),
        team_run_id=run.id,
        name="Competitor Brief",
        description=None,
        fields=FIELDS,
    )

    assert detail["slug"] == "competitor-brief"


async def test_a_second_app_of_the_same_name_still_gets_its_own_handle(wired: Any) -> None:
    """``uq_engine_apps_org_slug`` is unique per organisation. Two apps named the same must both
    save — the second takes the next rung of the ladder rather than failing the person's save."""
    service, runs, _ = wired
    first_run = await _finished_run(runs, ORG_A)
    second_run = await _finished_run(runs, ORG_A)

    first, _ = await service.create_from_run(
        _principal(ORG_A), team_run_id=first_run.id, name="Brief", description=None, fields=FIELDS
    )
    second, _ = await service.create_from_run(
        _principal(ORG_A), team_run_id=second_run.id, name="Brief", description=None, fields=FIELDS
    )

    assert first["slug"] != second["slug"]
    assert second["slug"] is not None


async def test_no_credential_survives_into_the_stored_copy(wired: Any) -> None:
    """The freeze is not optional here. It is the same scrub #932 already proved for the seeded app,
    and it has to hold for a copy taken from a run that was bound to the author's own key."""
    from oraclous_execution_engine_service.core.rls import org_scope

    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)

    detail, _ = await service.create_from_run(
        _principal(ORG_A), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
    )

    with org_scope(ORG_A):
        row = await service._apps.get(detail["id"], ORG_A)
    assert row is not None
    assert "credential_id" not in repr(row.manifest)
    assert "credential_mappings" not in repr(row.manifest)
    assert row.manifest["models"][0]["config"]["temperature"] == 0.2  # settings survive


async def test_the_app_keeps_its_own_copy_when_the_run_row_moves_on(wired: Any) -> None:
    """An app owns its documents rather than pointing at anything. Nothing that happens to the run
    afterwards may reach the app its users are running."""
    from oraclous_execution_engine_service.core.rls import org_scope

    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)
    detail, _ = await service.create_from_run(
        _principal(ORG_A), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
    )

    with org_scope(ORG_A):
        await runs.checkpoint(run.id, ORG_A, manifest={"ohm_version": "1.1", "members": []})
        row = await service._apps.get(detail["id"], ORG_A)

    assert row is not None
    assert row.manifest["metadata"]["name"] == "competitor-brief"


async def test_saving_the_same_run_twice_returns_the_one_app(wired: Any) -> None:
    """There is no delete endpoint in this issue, so a double-submitted save would leave a
    duplicate nobody can remove. The second call returns what the first made, exactly as
    ``team-drafts/from-run`` already does for a reloaded compile."""
    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)

    first, first_created = await service.create_from_run(
        _principal(ORG_A), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
    )
    second, second_created = await service.create_from_run(
        _principal(ORG_A), team_run_id=run.id, name="Brief again", description=None, fields=FIELDS
    )

    assert first_created is True
    assert second_created is False
    assert second["id"] == first["id"]


# ── what it refuses ──────────────────────────────────────────────────────────


async def test_a_run_that_did_not_succeed_is_refused_and_stores_nothing(wired: Any) -> None:
    """ "A finished run" is the ruling, and the reason is the product one: every app should be
    something its author watched work."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A, state="FAILED")

    with pytest.raises(TeamRunError) as caught:
        await service.create_from_run(
            _principal(ORG_A), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
        )

    assert caught.value.status_code == 422
    apps, total = await service.list_for_org(_principal(ORG_A))
    assert total == 0


async def test_another_organisations_run_is_a_404(wired: Any) -> None:
    """Not a 403. Telling a caller that a run exists but is not theirs is an enumeration the
    engine's other reads already refuse to make."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    service, runs, _ = wired
    run = await _finished_run(runs, ORG_B)

    with pytest.raises(TeamRunError) as caught:
        await service.create_from_run(
            _principal(ORG_A), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
        )

    assert caught.value.status_code == 404


async def test_a_principal_with_no_organisation_is_refused(wired: Any) -> None:
    """Fail-closed tenancy (ADR-006). There is no organisation to stamp the app with, so there is
    no app to make."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)

    with pytest.raises(TeamRunError) as caught:
        await service.create_from_run(
            _principal(None), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
        )

    assert caught.value.status_code == 403


async def test_two_fields_sharing_a_name_are_refused_at_the_save(wired: Any) -> None:
    """The person edited these, so a repeated label is an authoring mistake worth naming while they
    can still fix it — and with no update endpoint in this issue, an app saved with two identical
    labels cannot be corrected afterwards."""
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)

    with pytest.raises(TeamRunError) as caught:
        await service.create_from_run(
            _principal(ORG_A),
            team_run_id=run.id,
            name="Brief",
            description=None,
            fields=[
                {"name": "Focus", "hint": "a", "type": "short_text"},
                {"name": "Focus", "hint": "b", "type": "short_text"},
            ],
        )

    assert caught.value.status_code == 422


async def test_a_caller_cannot_make_an_app_owned_by_the_platform(wired: Any) -> None:
    """The catalogue-poison case. ``organisation_id`` comes from the principal, never the request,
    so there is no argument a caller could pass to claim the shared shelf."""
    service, runs, _ = wired
    run = await _finished_run(runs, ORG_A)

    detail, _ = await service.create_from_run(
        _principal(ORG_A), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
    )

    assert detail["origin"] == "organisation"
    platform_view, total = await service.list_for_org(_principal(PLATFORM_ORG))
    assert all(app["id"] != detail["id"] for app in platform_view)


# ── and what running it sends ────────────────────────────────────────────────


async def test_running_the_app_folds_the_fields_into_the_teams_one_input(wired: Any) -> None:
    """The fold, at the seam that performs it. The team declares ``task`` and nothing else, and the
    engine refuses any key outside that — so the three invented fields have to arrive as one
    request or the run never starts."""
    service, runs, recorder = wired
    run = await _finished_run(runs, ORG_A)
    detail, _ = await service.create_from_run(
        _principal(ORG_A), team_run_id=run.id, name="Brief", description=None, fields=FIELDS
    )

    await service.run(
        detail["id"],
        _principal(ORG_A),
        inputs={"competitor": "Acme Cloud", "focus": "their pricing move"},
        models=[{"binding": "default", "provider": "openrouter", "model": "openai/gpt-4o"}],
    )

    sent = recorder.calls[-1]["inputs"]
    assert set(sent) == {"task"}
    assert sent["task"] == "Competitor: Acme Cloud\nFocus: their pricing move"
