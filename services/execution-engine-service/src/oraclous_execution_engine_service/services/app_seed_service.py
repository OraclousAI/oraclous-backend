"""Seeding the Oraclous-provided apps (services layer, #932).

Called once per engine boot. It writes into the PLATFORM organisation, and the widened read on
``engine_apps`` does the rest — every tenant then sees these apps with nothing provisioned per
tenant, which is the whole point.

Two constraints shape it, and neither is optional:

* it must run inside ``org_scope(platform_org_id)``. The table's ``WITH CHECK`` is the strict
  caller-org equality, so an INSERT stamped with the platform organisation is admitted only when the
  bound org GUC matches it. Without the scope this raises 42501 under the ``oraclous_app`` runtime
  role — which is exactly the protection that stops a tenant planting a shared app.
* it must be idempotent by CONTENT. Every replica re-seeds on every boot; rewriting an unchanged row
  would have replicas racing on a row nobody asked to change. The repository compares fingerprints,
  so an unchanged app is not written at all.
"""

from __future__ import annotations

import logging
import uuid

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.seed_apps import build_seed_apps
from oraclous_execution_engine_service.repositories.app_repository import AppRepository

logger = logging.getLogger(__name__)

#: The author recorded on an Oraclous-provided app. A fixed platform identity, not a real person —
#: nobody's account should own a row every organisation reads.
PLATFORM_USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")


async def seed_platform_apps(
    repository: AppRepository, *, platform_org_id: uuid.UUID
) -> list[uuid.UUID]:
    """Write (or refresh) every Oraclous-provided app. Returns the ids that now exist."""
    seeded: list[uuid.UUID] = []
    with org_scope(platform_org_id):
        for app in build_seed_apps(platform_org_id):
            row = await repository.upsert_platform_app(
                slug=app.slug,
                name=app.name,
                description=app.description,
                user_id=PLATFORM_USER_ID,
                manifest=app.manifest,
                sub_harnesses=app.sub_harnesses,
                app_id=app.app_id,
            )
            seeded.append(row.id)
    return seeded
