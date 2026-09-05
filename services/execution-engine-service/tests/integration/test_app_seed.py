"""Seeding the Oraclous-provided apps at startup (#932) — docker-required.

The Validation Desk reaches every organisation because the engine seeds it into the platform
organisation on boot, and the widened read does the rest. That seed runs on EVERY start, on every
replica, so its correctness is mostly about what it does the second, third and hundredth time.

Three properties, each of which is a real failure if missing: the app keeps a stable id (the console
deep-links to it, and a new id per boot would break every saved link); an unchanged manifest writes
nothing (otherwise two replicas booting together race on a row nobody asked to change); and a
changed manifest is picked up rather than silently ignored.

RED until the seed service lands; the seams are imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytestmark = [pytest.mark.integration]

PLATFORM_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
async def app_repository(engine_dsns) -> AsyncIterator[Any]:  # noqa: ANN001
    from oraclous_execution_engine_service.repositories.app_repository import AppRepository

    _owner_async_dsn, app_async_dsn = engine_dsns
    repo = AppRepository(app_async_dsn, platform_org_id=PLATFORM_ORG)
    try:
        yield repo
    finally:
        await repo.close()


async def test_the_validation_desk_is_there_after_one_seed(app_repository: Any) -> None:
    from oraclous_execution_engine_service.services.app_seed_service import seed_platform_apps
    from oraclous_substrate.access import org_scope

    await seed_platform_apps(app_repository, platform_org_id=PLATFORM_ORG)

    with org_scope(ORG_A):
        rows, total = await app_repository.list_for_org(ORG_A)

    assert total == 1
    assert rows[0]["name"]
    assert rows[0]["organisation_id"] == PLATFORM_ORG


async def test_seeding_twice_leaves_one_app_with_the_same_id(app_repository: Any) -> None:
    """Every boot re-seeds. A second run must be a no-op, not a duplicate tile in everyone's tab."""
    from oraclous_execution_engine_service.services.app_seed_service import seed_platform_apps
    from oraclous_substrate.access import org_scope

    await seed_platform_apps(app_repository, platform_org_id=PLATFORM_ORG)
    with org_scope(ORG_A):
        first, _ = await app_repository.list_for_org(ORG_A)

    await seed_platform_apps(app_repository, platform_org_id=PLATFORM_ORG)
    with org_scope(ORG_A):
        second, total = await app_repository.list_for_org(ORG_A)

    assert total == 1
    assert second[0]["id"] == first[0]["id"]  # the console's deep link still resolves
    assert second[0]["updated_at"] == first[0]["updated_at"]  # nothing was rewritten


async def test_the_seeded_app_carries_no_credential(app_repository: Any) -> None:
    """A platform app is readable by every organisation, so it must ship with no key and no
    credential identifier at all. This is also what forces a run to supply the caller's own key —
    the app simply has nothing to run on otherwise."""
    import json

    from oraclous_execution_engine_service.services.app_seed_service import seed_platform_apps
    from oraclous_substrate.access import org_scope

    await seed_platform_apps(app_repository, platform_org_id=PLATFORM_ORG)

    with org_scope(ORG_A):
        rows, _ = await app_repository.list_for_org(ORG_A)
        app = await app_repository.get(rows[0]["id"], ORG_A)

    documents = json.dumps({"m": app.manifest, "s": app.sub_harnesses})
    assert "credential_id" not in documents
    assert "credential_mappings" not in documents


async def test_the_seeded_app_carries_its_members_inline(app_repository: Any) -> None:
    """A seeded app must not depend on agents filed in a registry, because resolving those would be
    a cross-organisation lookup on every tenant's run. Holding the member documents inline is what
    makes a platform app runnable in a fresh organisation with nothing provisioned."""
    from oraclous_execution_engine_service.services.app_seed_service import seed_platform_apps
    from oraclous_substrate.access import org_scope

    await seed_platform_apps(app_repository, platform_org_id=PLATFORM_ORG)

    with org_scope(ORG_A):
        rows, _ = await app_repository.list_for_org(ORG_A)
        app = await app_repository.get(rows[0]["id"], ORG_A)

    roles = {m["role"] for m in app.manifest["members"]}
    assert roles, "the seeded team has members"
    assert roles <= set(app.sub_harnesses), "every member's document travels with the app"


async def test_a_changed_manifest_is_picked_up_and_bumps_the_pinned_version(
    app_repository: Any,
) -> None:
    """The other half of idempotency. When Oraclous ships a better version of its own app, the seed
    must actually update it — and say so, so the change is visible rather than silent."""
    from oraclous_execution_engine_service.services.app_seed_service import seed_platform_apps
    from oraclous_substrate.access import org_scope

    await seed_platform_apps(app_repository, platform_org_id=PLATFORM_ORG)
    with org_scope(ORG_A):
        rows, _ = await app_repository.list_for_org(ORG_A)
        before = await app_repository.get(rows[0]["id"], ORG_A)

    edited = dict(before.manifest)
    edited["members"] = [
        {**edited["members"][0], "subgoal": "gather evidence, and name what it does not cover"},
        *edited["members"][1:],
    ]
    with org_scope(PLATFORM_ORG):
        await app_repository.upsert_platform_app(
            slug=before.slug,
            name=before.name,
            description=before.description,
            user_id=before.user_id,
            manifest=edited,
            sub_harnesses=before.sub_harnesses,
        )

    with org_scope(ORG_A):
        after = await app_repository.get(before.id, ORG_A)

    assert after.id == before.id
    assert after.pinned_version == before.pinned_version + 1
    assert after.manifest_fingerprint != before.manifest_fingerprint
