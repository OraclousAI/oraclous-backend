"""Integration: Google Drive Reader retirement (#770) vs real Postgres.

The Drive plugin advertised ``list_files``/``read_file`` and could execute neither (no executor,
no connector module), so #770 removes it from the catalogue. Because ``sync_plugins`` is
upsert-only and ``tool_instances.descriptor_id`` is ``ondelete=RESTRICT``, removing the plugin
class alone leaves already-seeded orgs offering a dead connector — so sync must also RETIRE the
seeded descriptor: delete its instances (never executable, so no working configuration is lost),
then the descriptor row, idempotently, without touching any other descriptor.

The retirement seam extends ``sync_plugins`` with an ``instance_repository`` keyword; existing
callers without legacy Drive state stay valid, so the keyword is optional in the contract.

The retired Drive descriptor id is pinned literally: after the ``[impl]`` lands the plugin class
no longer exists to derive it from.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )
    from oraclous_capability_registry_service.repositories.instance_repository import (
        InstanceRepository,
    )

pytestmark = pytest.mark.integration

_DEV_ORG = "00000000-0000-0000-0000-00000000050a"
_USER = "00000000-0000-0000-0000-0000000000c5"
#: GoogleDriveReaderPlugin.plugin_id() as of its removal — the deterministic seeded descriptor id.
_DRIVE_DESCRIPTOR_ID = uuid.UUID("d5c5a63d-0979-59e7-b82c-46714c204d4e")

Repos = tuple["CapabilityRepository", "InstanceRepository"]


@pytest.fixture
async def repos(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Repos]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    monkeypatch.setenv("DATABASE_URL", async_dsn)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "dev-internal-key")
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("DEV_BEARER", "dev-token")
    monkeypatch.setenv("DEV_ORG_ID", _DEV_ORG)
    from oraclous_capability_registry_service.core.config import get_settings

    get_settings.cache_clear()

    from oraclous_capability_registry_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup_engine = create_async_engine(async_dsn)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    from oraclous_capability_registry_service.repositories.capability_repository import (
        CapabilityRepository,
    )
    from oraclous_capability_registry_service.repositories.instance_repository import (
        InstanceRepository,
    )

    cap_repo = CapabilityRepository(async_dsn)
    inst_repo = InstanceRepository(async_dsn)
    yield cap_repo, inst_repo
    await cap_repo.close()
    await inst_repo.close()
    get_settings.cache_clear()


async def _seed_legacy_drive_state(
    cap_repo: CapabilityRepository, inst_repo: InstanceRepository
) -> uuid.UUID:
    """Recreate what an old seed left behind: the Drive descriptor plus a configured instance."""
    from oraclous_capability_registry_service.models.enums import DescriptorKind, InstanceStatus

    org = uuid.UUID(_DEV_ORG)
    await cap_repo.upsert_by_id(
        organisation_id=org,
        descriptor_id=_DRIVE_DESCRIPTOR_ID,
        kind=DescriptorKind.TOOL,
        descriptor={
            "metadata": {"name": "Google Drive Reader"},
            "spec": {"capabilities": [{"name": "read_file"}]},
        },
    )
    row = await inst_repo.create(
        organisation_id=org,
        capability_id=_DRIVE_DESCRIPTOR_ID,
        user_id=uuid.UUID(_USER),
        name="my drive",
        description=None,
        configuration={},
        settings={},
        required_credentials=["oauth_token"],
        status=InstanceStatus.CONFIGURATION_REQUIRED,
    )
    return uuid.UUID(str(row.id))


async def test_sync_retires_the_seeded_drive_descriptor_and_its_instances(repos: Repos) -> None:
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    cap_repo, inst_repo = repos
    org = uuid.UUID(_DEV_ORG)
    await _seed_legacy_drive_state(cap_repo, inst_repo)

    statuses = await sync_plugins(
        repository=cap_repo, instance_repository=inst_repo, organisation_id=org
    )

    assert statuses.get(str(_DRIVE_DESCRIPTOR_ID)) == "retired"
    listed = await cap_repo.list_by_org(org)
    assert _DRIVE_DESCRIPTOR_ID not in {d.id for d in listed}
    instances = await inst_repo.list_by_org(org)
    assert all(i.capability_id != _DRIVE_DESCRIPTOR_ID for i in instances)


async def test_retirement_is_idempotent_and_leaves_other_descriptors_alone(repos: Repos) -> None:
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    cap_repo, inst_repo = repos
    org = uuid.UUID(_DEV_ORG)
    await _seed_legacy_drive_state(cap_repo, inst_repo)

    await sync_plugins(repository=cap_repo, instance_repository=inst_repo, organisation_id=org)
    survivors = {d.id for d in await cap_repo.list_by_org(org)}
    second = await sync_plugins(
        repository=cap_repo, instance_repository=inst_repo, organisation_id=org
    )

    # a re-run with nothing left to retire is a no-op: every live descriptor unchanged, none lost
    assert {d.id for d in await cap_repo.list_by_org(org)} == survivors
    assert _DRIVE_DESCRIPTOR_ID not in survivors
    live_statuses = {v for k, v in second.items() if k != str(_DRIVE_DESCRIPTOR_ID)}
    assert live_statuses <= {"unchanged"}


async def test_google_drive_reader_is_not_in_the_tool_catalogue(repos: Repos) -> None:
    """The catalogue a fresh org sees carries no connector that cannot execute (#770).

    Deliberately calls ``sync_plugins`` with its pre-#770 signature so this test is RED on the
    catalogue assertion itself, not on the new seam's keyword.
    """
    from oraclous_capability_registry_service.app.factory import create_app
    from oraclous_capability_registry_service.services.plugin_sync import sync_plugins

    cap_repo, inst_repo = repos
    await sync_plugins(repository=cap_repo, organisation_id=uuid.UUID(_DEV_ORG))
    app = create_app(lifespan=None)
    app.state.capability_repository = cap_repo
    app.state.instance_repository = inst_repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cr.test") as client:
        resp = await client.get("/api/v1/tools", headers={"Authorization": "Bearer dev-token"})
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in resp.json()["capabilities"]}
    assert "Google Drive Reader" not in names
    # the working readers stay listed
    assert {"GitHub Reader", "Notion Reader"} <= names
