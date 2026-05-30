"""ADR-006 reshape: ``organisation_id`` is the outermost scope on the ReBAC
engine (ORA-34).

These are the NEW org-scoping assertions layered on top of the lifted legacy
behaviour (``test_rebac_engine.py``). They pin the reshape the extraction adds:

* ``organisation_id`` is required on every engine entry point — a blank value is
  a programming error and must raise, never silently allow (mirrors the legacy
  ``if not graph_id: raise ValueError`` guard, now also for the org scope).
* every Cypher query the engine issues carries ``organisation_id`` as a bound
  parameter, so relation edges (HAS_ROLE, CAN_ACCESS, …) are written and
  filtered by it — closing the tenant loop (Threat T1).
* one organisation's call never leaks another organisation's id into its query
  params (the cross-org-returns-0 invariant, asserted at the data layer in
  ``tests/organization_isolation/test_rebac_org_edges.py``).
* the permission cache key is namespaced by ``organisation_id`` so a cached
  decision in one org can never satisfy a check in another.

RED until ``backend-implementer`` adds ``organisation_id`` scoping to
``oraclous_rebac.ReBACEngine``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from oraclous_rebac import ReBACEngine

pytestmark = [pytest.mark.unit, pytest.mark.rebac]

_ORG_A = "org-aaaa"
_OTHER_ORG = "org-bbbb"
_BLANK = ["", "   "]


def _null_redis():
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.return_value = True
    redis.delete.return_value = 1
    return redis


def _make_driver(single_return=None):
    session = AsyncMock()
    result = AsyncMock()
    result.single = AsyncMock(return_value=single_return)

    async def _aiter(_self):
        for row in []:
            yield row

    result.__aiter__ = _aiter
    session.run = AsyncMock(return_value=result)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session.return_value = cm
    return driver, session


def _resolving_session():
    """A session whose three sequential queries resolve a full check (PhaseB+PhaseA)."""
    perm = AsyncMock()
    perm.single = AsyncMock(return_value={"authorized": False})
    role = AsyncMock()
    role.single = AsyncMock(return_value={"cnt": 0})
    phase_a = AsyncMock()
    phase_a.single = AsyncMock(return_value={"authorized": False})
    session = AsyncMock()
    session.run = AsyncMock(side_effect=[perm, role, phase_a])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session.return_value = cm
    return driver, session


def _engine() -> ReBACEngine:
    engine = ReBACEngine()
    engine._redis = _null_redis()
    return engine


def _params_of(session) -> list[dict]:
    return [call[0][1] if len(call[0]) > 1 else {} for call in session.run.call_args_list]


# ── organisation_id is required everywhere ─────────────────────────────────


class TestOrganisationIdRequired:
    @pytest.mark.parametrize("blank", _BLANK)
    async def test_check_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=blank,
                subject={"type": "user", "id": "user-a"},
                graph_id="graph-1",
                required_level="read",
            )

    @pytest.mark.parametrize("blank", _BLANK)
    async def test_grant_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.grant_role(
                driver,
                organisation_id=blank,
                graph_id="graph-1",
                target_user_id="user-a",
                role_name="viewer",
                granted_by="admin",
            )

    @pytest.mark.parametrize("blank", _BLANK)
    async def test_revoke_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.revoke_role(
                driver,
                organisation_id=blank,
                graph_id="graph-1",
                target_user_id="user-a",
                role_name="viewer",
            )

    @pytest.mark.parametrize("blank", _BLANK)
    async def test_list_members_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.list_graph_members(driver, organisation_id=blank, graph_id="graph-1")

    @pytest.mark.parametrize("blank", _BLANK)
    async def test_create_subgraph_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.create_subgraph(
                driver, organisation_id=blank, graph_id="graph-1", name="sg"
            )


# ── organisation_id is bound into every query ──────────────────────────────


class TestOrganisationIdBoundToQueries:
    async def test_check_threads_organisation_id_into_every_query(self) -> None:
        engine = _engine()
        driver, session = _resolving_session()
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject={"type": "user", "id": "user-a"},
            graph_id="graph-1",
            required_level="read",
        )
        all_params = _params_of(session)
        assert all_params, "engine issued no queries"
        for params in all_params:
            assert params.get("organisation_id") == _ORG_A, (
                f"a check query did not bind organisation_id: {params}"
            )

    async def test_grant_binds_organisation_id(self) -> None:
        engine = _engine()
        driver, session = _make_driver(single_return=None)
        await engine.grant_role(
            driver,
            organisation_id=_ORG_A,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="viewer",
            granted_by="admin",
        )
        assert any(p.get("organisation_id") == _ORG_A for p in _params_of(session))

    async def test_revoke_binds_organisation_id(self) -> None:
        engine = _engine()
        driver, session = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_role(
            driver,
            organisation_id=_ORG_A,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="viewer",
        )
        assert any(p.get("organisation_id") == _ORG_A for p in _params_of(session))

    async def test_bootstrap_binds_organisation_id_on_every_query(self) -> None:
        engine = _engine()
        session = AsyncMock()
        result_mock = AsyncMock()
        result_mock.single = AsyncMock(return_value={"role_id": "id"})
        session.run = AsyncMock(return_value=result_mock)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        driver = MagicMock()
        driver.session.return_value = cm

        await engine.bootstrap_graph_roles(
            driver,
            organisation_id=_ORG_A,
            graph_id="graph-1",
            owner_user_id="user-a",
        )
        params = _params_of(session)
        assert params
        for p in params:
            assert p.get("organisation_id") == _ORG_A, f"bootstrap query missing org scope: {p}"


# ── cross-org isolation at the parameter boundary ──────────────────────────


class TestCrossOrgParamIsolation:
    async def test_check_never_leaks_another_org_id(self) -> None:
        engine = _engine()
        driver, session = _resolving_session()
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject={"type": "user", "id": "user-a"},
            graph_id="graph-1",
            required_level="read",
        )
        for params in _params_of(session):
            assert _OTHER_ORG not in str(params)

    async def test_grant_never_leaks_another_org_id(self) -> None:
        engine = _engine()
        driver, session = _make_driver(single_return=None)
        await engine.grant_role(
            driver,
            organisation_id=_ORG_A,
            graph_id="graph-1",
            target_user_id="user-x",
            role_name="viewer",
            granted_by="admin-A",
        )
        for params in _params_of(session):
            assert _OTHER_ORG not in str(params)


# ── cache key is namespaced by organisation_id ─────────────────────────────


class TestCacheIsOrgScoped:
    async def test_resolved_decision_caches_under_org_namespaced_key(self) -> None:
        """The key the engine reads/writes in Redis is namespaced by organisation_id.

        Guards against a cached allow/deny in one org satisfying a check in
        another (a cross-org cache-poisoning path).
        """
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _ = _resolving_session()
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject={"type": "user", "id": "user-a"},
            graph_id="graph-1",
            required_level="read",
        )
        # the cache lookup (get) keys on the organisation_id
        get_keys = " ".join(str(c) for c in redis.get.call_args_list)
        assert _ORG_A in get_keys, f"cache key not namespaced by organisation_id: {get_keys}"

    async def test_revoke_invalidation_targets_org_namespaced_keys(self) -> None:
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_role(
            driver,
            organisation_id=_ORG_A,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="editor",
        )
        deleted = " ".join(str(c) for c in redis.delete.call_args_list)
        assert _ORG_A in deleted

    async def test_grant_invalidation_targets_org_namespaced_keys(self) -> None:
        """Grant invalidation is org-scoped too — it must not clear another org's keys."""
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _ = _make_driver(single_return=None)
        await engine.grant_role(
            driver,
            organisation_id=_ORG_A,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="viewer",
            granted_by="admin",
        )
        deleted = " ".join(str(c) for c in redis.delete.call_args_list)
        assert deleted, "grant_role must invalidate the permission cache"
        assert _ORG_A in deleted
        assert _OTHER_ORG not in deleted
