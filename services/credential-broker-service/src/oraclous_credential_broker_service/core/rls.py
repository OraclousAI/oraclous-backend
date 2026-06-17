"""Postgres RLS wiring for the credential-broker (ADR-030, core connection layer).

The substrate carries the generic seam (``oraclous_substrate.access_async``); this
module is the service-local wiring that activates the row-level-security backstop
for the broker's four org-scoped tables (``user_credentials``, ``webhook_secrets``,
``delegated_tokens``, ``org_data_keys``):

* :func:`build_rls_engine` — the ONE way the service constructs a runtime
  ``AsyncEngine``. It installs the substrate ``begin``-event guard so **every**
  transaction on that engine binds ``app.current_organisation_id`` transaction-locally
  from the bound ``OrganisationContext`` (and fails closed to the empty GUC — zero
  rows — when no context is bound). Every repository builds its engine through here.

* :func:`org_scope` — the repository-side chokepoint: a repo method binds the org it
  already received from authenticated context (the request principal on the user
  surface, the trusted caller's ``organisation_id`` on the X-Internal-Key surface,
  or the task org in a worker) for the duration of the DB operation, so the engine
  guard reads it. RLS only needs the org; the principal id is a marker here (the GUC
  policy keys solely on ``organisation_id``).

* :func:`assert_runtime_role_isolates` — startup fail-closed: refuse to come up under
  a superuser / ``BYPASSRLS`` role, which would silently void the backstop (T1-M3).

The privileged operator paths — Alembic migrations and the envelope backfill sweep
(which reads every org's ciphertext) — connect on the **owner** DSN, which is a
superuser in the dev stack and therefore bypasses RLS; the empty GUC the guard binds
there is irrelevant. Only the long-running runtime service switches its DSN to the
NOSUPERUSER ``oraclous_app`` role (ADR-030 §3).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

from oraclous_governance import (
    OrganisationContext,
    PrincipalType,
    use_organisation_context,
)
from oraclous_substrate.access_async import (
    RlsBypassingRoleError,
    assert_non_bypassing_role,
    install_org_guc_guard,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

__all__ = [
    "RlsBypassingRoleError",
    "assert_runtime_role_isolates",
    "build_rls_engine",
    "org_scope",
]

# A fixed marker principal for the GUC-binding context on paths where the principal
# is not the unit of scope (the trusted-caller / worker DB paths). RLS keys only on
# organisation_id, so the principal value is never consulted by the policy; this keeps
# org_scope usable from a repository that holds the org but not a full principal.
_RLS_PRINCIPAL = uuid.UUID("00000000-0000-0000-0000-0000000005d5")


def build_rls_engine(db_url: str, **engine_kwargs: Any) -> AsyncEngine:
    """Create an ``AsyncEngine`` with the RLS org-GUC guard installed (ADR-030 §2).

    The single constructor every repository uses so no runtime engine can be created
    without the per-transaction org binding. Engine kwargs (``echo``, ``pool_pre_ping``)
    pass through unchanged.
    """
    engine = create_async_engine(db_url, **engine_kwargs)
    install_org_guc_guard(engine)
    return engine


@contextlib.contextmanager
def org_scope(organisation_id: uuid.UUID) -> Iterator[None]:
    """Bind the org for the enclosed DB operation so the engine guard sets the GUC.

    Idempotent w.r.t. nesting (``use_organisation_context`` restores the prior
    binding on exit). The org is sourced from authenticated context by the caller —
    never a request body — preserving the fail-closed T1-M1 invariant.
    """
    context = OrganisationContext(
        organisation_id=organisation_id,
        principal_id=_RLS_PRINCIPAL,
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )
    with use_organisation_context(context):
        yield


async def assert_runtime_role_isolates(engine: AsyncEngine) -> None:
    """Fail closed at startup unless the runtime role is NOSUPERUSER/NOBYPASSRLS.

    The async mirror of ``scoped_pg_connection``'s role chokepoint (ADR-030 §3 / T1-M3):
    a bypassing role makes the RLS policy inert, so the service must refuse to start
    rather than run with a void backstop. Delegates to the substrate assertion.
    """
    await assert_non_bypassing_role(engine)
