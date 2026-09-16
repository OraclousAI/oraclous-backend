"""#1072 — the cross-replica cancel lease, vs real Postgres.

``ExecutionLeaseRepository`` (``repositories/execution_lease_repository.py``, not yet built) backs
the cancel signal that has to cross Helm's ``replicas: 2`` (design: no Redis in harness-runtime, so
an in-process registry is wrong). One row per in-flight execution, keyed by the GLOBALLY unique
``execution_id`` (the engine mints it once per dispatch and a duplicate is a 409 — see #1072 design
ruling, "Identifier"): ``harness_execution_leases(execution_id PK, organisation_id, created_at,
cancel_requested_at NULL)``, RLS-scoped like migration 0006.

Runs the repository itself against the real NOSUPERUSER/NOBYPASSRLS ``oraclous_app`` role via the
``harness_dsns`` fixture chain (``tests/conftest.py``), the same role/engine-guard pattern
``tests/organization_isolation/test_harness_rls_backstop_isolation.py`` uses for the other four
harness tables — a superuser DSN would bypass FORCE'd RLS entirely and hide a repository that never
binds its org (#1072 Tests Review B1). ``tests/organization_isolation/...`` already proves the raw
table-level policy directly against the schema; this file proves the *repository's own* org_scope
wiring on top of it — the layer an app-layer bug (a dropped ``org_scope`` call) would actually
break.

Proves:

* ``create`` + ``request_cancel`` under the owning org flips the flag and ``is_cancel_requested``
  (now org-scoped, like every other lease method) sees it;
* ``request_cancel`` under a DIFFERENT org for the SAME execution_id returns False and never sets
  the flag (H1/H4 — a forged/guessed id from another org can't request a cancel);
* a second ``create`` for an execution_id already claimed raises ``DuplicateExecutionId`` — the
  global-PK conflict the service maps to 409 on ``execute()``;
* ``release`` (now org-scoped) deletes the row outright under the OWNING org — the watcher's cleanup
  after the terminal row lands;
* org B calling ``is_cancel_requested`` / ``release`` on org A's execution_id sees False / deletes
  nothing under the real RLS-enforcing role, and org A's own flag and row survive untouched — the
  scenario a signature with no ``organisation_id`` parameter cannot even express, let alone pass
  under the real app role (#1072 Tests Review B1: the FORCE'd policy fails an unbound/wrong org
  closed to zero rows, so an org-blind watcher would never see its own flag in production).

Key-free: a testcontainer Postgres; the repo self-binds the org (ADR-030 ``org_scope``), same
pattern as ``test_run_tree_correlation.py``. Every name new to this slice (the module, both classes,
all four repository methods, the table itself) is imported/referenced function-locally per
``.claude/rules/tests-seam-imports.md`` — this hard-fails RED (``ModuleNotFoundError``) until the
``[impl]`` lands, never a skip. ``harness_dsns`` itself hard-fails RED (``UndefinedTable``) until
migration 0010 adds ``harness_execution_leases`` — see ``tests/conftest.py``'s module docstring.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG_A = uuid.UUID("00000000-0000-0000-0000-000000001072")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-000000002072")


def _row_count(dsn: str, execution_id: uuid.UUID) -> int:
    """Direct-SQL proof that ``release`` actually deleted the row (not just that the repository API
    stopped reporting it) — the superuser owner DSN, used only to observe, never to bypass the
    repository under test."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM harness_execution_leases WHERE execution_id = %s",
            (execution_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


@pytest.fixture
async def repo(harness_dsns) -> AsyncIterator[object]:  # noqa: ANN001
    """The repository built on the NOSUPERUSER ``oraclous_app`` DSN — RLS actually bites. Schema,
    RLS enablement (all five harness tables, ``harness_execution_leases`` included — #1072) and the
    app role's GRANTs are already provisioned by ``harness_dsns`` as the superuser owner."""
    _owner_async_dsn, app_async_dsn = harness_dsns

    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        ExecutionLeaseRepository,
    )

    r = ExecutionLeaseRepository(app_async_dsn)
    yield r
    await r.close()  # type: ignore[attr-defined]


async def test_create_then_request_cancel_under_owning_org_sets_the_flag(repo: object) -> None:
    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]

    assert await repo.is_cancel_requested(execution_id, _ORG_A) is False  # type: ignore[attr-defined]  # not requested yet

    flipped = await repo.request_cancel(  # type: ignore[attr-defined]
        execution_id=execution_id, organisation_id=_ORG_A
    )
    assert flipped is True
    assert await repo.is_cancel_requested(execution_id, _ORG_A) is True  # type: ignore[attr-defined]


@pytest.mark.security
async def test_request_cancel_under_a_different_org_is_refused_and_flag_stays_unset(
    repo: object,
) -> None:
    """H1/H4: org B guessing/forging org A's execution_id cannot request a cancel on it."""
    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]

    flipped = await repo.request_cancel(  # type: ignore[attr-defined]
        execution_id=execution_id, organisation_id=_ORG_B
    )
    assert flipped is False
    # still unset — checked under the TRUE owning org so a False can't be an artifact of asking
    # the wrong org itself.
    assert await repo.is_cancel_requested(execution_id, _ORG_A) is False  # type: ignore[attr-defined]


async def test_duplicate_execution_id_raises(repo: object) -> None:
    from oraclous_harness_runtime_service.repositories.execution_lease_repository import (
        DuplicateExecutionId,
    )

    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]

    with pytest.raises(DuplicateExecutionId):
        await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]


async def test_release_deletes_the_row_under_owning_org(repo: object, postgres_dsn: str) -> None:
    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]
    assert _row_count(postgres_dsn, execution_id) == 1

    await repo.release(execution_id, _ORG_A)  # type: ignore[attr-defined]

    assert _row_count(postgres_dsn, execution_id) == 0
    # a released id no longer claims anything — request_cancel finds no row for any org.
    assert (
        await repo.request_cancel(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]
    ) is False


@pytest.mark.security
async def test_org_b_watcher_check_and_release_see_nothing_org_a_survives(
    repo: object, postgres_dsn: str
) -> None:
    """#1072 Tests Review B1: proven under the real FORCE'd-RLS ``oraclous_app`` role (via the
    ``repo`` fixture's ``harness_dsns``), never the superuser — a wrong-org caller of either method
    must see the SAME nothing a production replica bound to the wrong org would see: the watcher's
    poll reads False (never cancels), and a release deletes zero rows (never leaks another org's
    lease). Org A's own flag and row are untouched by org B's calls throughout."""
    execution_id = uuid.uuid4()
    await repo.create(execution_id=execution_id, organisation_id=_ORG_A)  # type: ignore[attr-defined]
    flipped = await repo.request_cancel(  # type: ignore[attr-defined]
        execution_id=execution_id, organisation_id=_ORG_A
    )
    assert flipped is True
    assert await repo.is_cancel_requested(execution_id, _ORG_A) is True  # type: ignore[attr-defined]

    # org B's watcher-shaped check sees nothing — RLS hides org A's row from org B's bound GUC.
    assert await repo.is_cancel_requested(execution_id, _ORG_B) is False  # type: ignore[attr-defined]

    # org B's release is a no-op under RLS: the row is invisible to it, so the DELETE affects zero
    # rows — org A's lease survives.
    await repo.release(execution_id, _ORG_B)  # type: ignore[attr-defined]
    assert _row_count(postgres_dsn, execution_id) == 1

    # org A's own flag and row are untouched by any of org B's calls.
    assert await repo.is_cancel_requested(execution_id, _ORG_A) is True  # type: ignore[attr-defined]
    await repo.release(execution_id, _ORG_A)  # type: ignore[attr-defined]
    assert _row_count(postgres_dsn, execution_id) == 0
