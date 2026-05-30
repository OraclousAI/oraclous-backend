"""Behavioural lift of the legacy ReBAC engine into ``packages/rebac`` (ORA-34).

Lift-tag **Extract + Reshape**. Behavioural reference:
``knowledge-graph-builder/app/services/rebac_service.py`` (``ReBACService``) plus
its legacy suites ``tests/unit/test_rebac_phase_b.py`` and
``tests/integration/test_rebac.py``. These tests lift the engine behaviour the
extraction MUST preserve — cache→Phase B→Phase A resolution order, fail-closed
traversal, the 60s permission cache, soft-revoke, and live expiry — onto the
extracted ``oraclous_rebac.ReBACEngine``.

The ADR-006 reshape (``organisation_id`` as the outermost scope on every edge
and every query) is asserted separately in ``test_organisation_scoping.py``;
here ``organisation_id`` is threaded through so these tests describe the
*reshaped* engine, but the assertions are about the lifted behaviour.

RED until ``backend-implementer`` creates ``oraclous_rebac.ReBACEngine``. The
legacy ``verify_graph_access`` FastAPI-dependency tests (403/404 leak, security
cases 7/8) are deliberately NOT lifted here — they belong to the consuming
service layer, not the Layer-1 engine.

Methods are called with keyword arguments so these tests pin the *contract*
(names + ``organisation_id`` scoping) without pinning positional order.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from oraclous_rebac import ReBACEngine

pytestmark = [pytest.mark.unit, pytest.mark.rebac]

_ORG = "org-aaaa"


def _make_driver(single_return=None, iter_return=None):
    """Build a mock async Neo4j driver, mirroring the legacy test helper.

    ``driver.session()`` is a *sync* call returning an async context manager, so
    ``session`` is an ``AsyncMock`` reached via a ``MagicMock`` context manager.
    """
    session = AsyncMock()

    result = AsyncMock()
    result.single = AsyncMock(return_value=single_return)

    async def _aiter(_self):
        for row in iter_return or []:
            yield row

    result.__aiter__ = _aiter
    session.run = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session.return_value = cm
    return driver, session, result


def _null_redis():
    redis = AsyncMock()
    redis.get.return_value = None  # cache miss
    redis.set.return_value = True
    redis.delete.return_value = 1
    return redis


def _phase_b_driver(perm_authorized: bool, role_count: int = 1):
    """A driver whose two sequential Phase B queries return (perm, role-count)."""
    perm_result = AsyncMock()
    perm_result.single = AsyncMock(return_value={"authorized": perm_authorized})
    role_result = AsyncMock()
    role_result.single = AsyncMock(return_value={"cnt": role_count})

    session = AsyncMock()
    session.run = AsyncMock(side_effect=[perm_result, role_result])

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


# ── Permission resolution — Phase B (HAS_ROLE traversal) ───────────────────


class TestPermissionCheckPhaseB:
    async def test_owner_can_read(self) -> None:
        """Owner with graph:read permission resolves True (Phase B authoritative)."""
        engine = _engine()
        driver, _ = _phase_b_driver(perm_authorized=True)
        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG,
            subject={"type": "user", "id": "owner-user"},
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is True

    async def test_viewer_cannot_write(self) -> None:
        """Viewer lacking graph:write resolves False."""
        engine = _engine()
        driver, _ = _phase_b_driver(perm_authorized=False)
        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG,
            subject={"type": "user", "id": "viewer-user"},
            graph_id="graph-1",
            required_level="write",
        )
        assert allowed is False

    async def test_phase_a_fallback_when_no_phase_b_data(self) -> None:
        """When no Role nodes exist (cnt=0), fall back to Phase A CAN_ACCESS."""
        engine = _engine()
        perm_result = AsyncMock()
        perm_result.single = AsyncMock(return_value={"authorized": False})
        role_result = AsyncMock()
        role_result.single = AsyncMock(return_value={"cnt": 0})  # no Phase B data
        phase_a_result = AsyncMock()
        phase_a_result.single = AsyncMock(return_value={"authorized": True})

        session = AsyncMock()
        session.run = AsyncMock(side_effect=[perm_result, role_result, phase_a_result])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        driver = MagicMock()
        driver.session.return_value = cm

        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG,
            subject={"type": "user", "id": "legacy-user"},
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is True


# ── Fail-closed, cache, injection safety ───────────────────────────────────


class TestPermissionCheckFailClosed:
    async def test_neo4j_error_fails_closed(self) -> None:
        """A Neo4j error denies (returns False) and never propagates."""
        engine = _engine()
        session = AsyncMock()
        session.run.side_effect = Exception("Neo4j connection lost")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        driver = MagicMock()
        driver.session.return_value = cm

        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG,
            subject={"type": "user", "id": "user-a"},
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is False

    async def test_cache_hit_skips_neo4j(self) -> None:
        """A Redis cache hit bypasses Neo4j entirely."""
        engine = ReBACEngine()
        redis = _null_redis()
        redis.get.return_value = "1"  # cache hit = authorized
        engine._redis = redis
        driver, session, _ = _make_driver()

        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG,
            subject={"type": "user", "id": "user-a"},
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is True
        session.run.assert_not_called()

    async def test_check_uses_parameterised_queries(self) -> None:
        """An injection string never appears as a literal in the Cypher text."""
        engine = _engine()
        perm_result = AsyncMock()
        perm_result.single = AsyncMock(return_value={"authorized": False})
        role_result = AsyncMock()
        role_result.single = AsyncMock(return_value={"cnt": 0})
        phase_a_result = AsyncMock()
        phase_a_result.single = AsyncMock(return_value={"authorized": False})
        session = AsyncMock()
        session.run = AsyncMock(side_effect=[perm_result, role_result, phase_a_result])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        driver = MagicMock()
        driver.session.return_value = cm

        injection = "admin'; DROP DATABASE neo4j; --"
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG,
            subject={"type": "user", "id": injection},
            graph_id="graph-1",
            required_level="read",
        )

        for call in session.run.call_args_list:
            query = call[0][0]
            assert injection not in query, "injection string must never appear in query text"


# ── graph_id validation (legacy T9) ────────────────────────────────────────


class TestGraphIdValidation:
    async def test_check_raises_without_graph_id(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=_ORG,
                subject={"type": "user", "id": "user-a"},
                graph_id="",
                required_level="read",
            )

    async def test_grant_raises_without_graph_id(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.grant_role(
                driver,
                organisation_id=_ORG,
                graph_id="",
                target_user_id="user-a",
                role_name="viewer",
                granted_by="admin-user",
            )

    async def test_revoke_raises_without_graph_id(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.revoke_role(
                driver,
                organisation_id=_ORG,
                graph_id="",
                target_user_id="user-a",
                role_name="viewer",
            )


# ── Role management — grant / revoke / soft-revoke / cache ─────────────────


class TestRoleManagement:
    async def test_grant_role_runs_merge_with_params(self) -> None:
        engine = _engine()
        driver, session, _ = _make_driver(single_return=None)
        await engine.grant_role(
            driver,
            organisation_id=_ORG,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="viewer",
            granted_by="admin-user",
            email="b@test.com",
        )
        assert session.run.called
        query, params = session.run.call_args_list[0][0][0], session.run.call_args_list[0][0][1]
        assert "MERGE" in query
        assert params["graph_id"] == "graph-1"
        assert params["role_name"] == "viewer"

    async def test_revoke_returns_zero_when_not_found(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver(single_return={"revoked_count": 0})
        count = await engine.revoke_role(
            driver,
            organisation_id=_ORG,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="viewer",
        )
        assert count == 0

    async def test_revoke_is_soft_not_a_delete(self) -> None:
        """Soft-revoke flips ``is_active`` to false; it must never DETACH/DELETE the edge."""
        engine = _engine()
        driver, session, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_role(
            driver,
            organisation_id=_ORG,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="editor",
        )
        query = session.run.call_args_list[0][0][0]
        assert "is_active" in query
        assert "DELETE" not in query.upper()

    async def test_revoke_invalidates_cache_for_user_and_graph(self) -> None:
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_role(
            driver,
            organisation_id=_ORG,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="editor",
        )
        assert redis.delete.called
        combined = " ".join(str(c) for c in redis.delete.call_args_list)
        assert "user-b" in combined
        assert "graph-1" in combined

    async def test_grant_invalidates_cache_for_user_and_graph(self) -> None:
        """A grant must invalidate the cache too (AC#3) — else a freshly granted

        role is masked by a stale deny for up to the 60s TTL. Legacy ``grant_role``
        calls ``invalidate_permission_cache`` (rebac_service.py:617).
        """
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _, _ = _make_driver(single_return=None)
        await engine.grant_role(
            driver,
            organisation_id=_ORG,
            graph_id="graph-1",
            target_user_id="user-b",
            role_name="viewer",
            granted_by="admin-user",
        )
        assert redis.delete.called, "grant_role must invalidate the permission cache"
        combined = " ".join(str(c) for c in redis.delete.call_args_list)
        assert "user-b" in combined
        assert "graph-1" in combined


# ── Bootstrap, members, subgraph ───────────────────────────────────────────


class TestBootstrapGraphRoles:
    async def test_bootstrap_runs_at_least_one_query_per_system_role(self) -> None:
        from oraclous_rebac import _SYSTEM_ROLES

        engine = _engine()
        session = AsyncMock()
        result_mock = AsyncMock()
        result_mock.single = AsyncMock(return_value={"role_id": "some-id"})
        session.run = AsyncMock(return_value=result_mock)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        driver = MagicMock()
        driver.session.return_value = cm

        await engine.bootstrap_graph_roles(
            driver,
            organisation_id=_ORG,
            graph_id="graph-1",
            owner_user_id="user-a",
        )
        assert session.run.call_count >= len(_SYSTEM_ROLES) + 1


class TestListGraphMembers:
    async def test_returns_empty_list_when_no_members(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver(iter_return=[])
        members = await engine.list_graph_members(driver, organisation_id=_ORG, graph_id="graph-1")
        assert members == []


class TestSubGraphManagement:
    async def test_create_subgraph_returns_required_keys(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver(
            single_return={
                "subgraph_id": "sg-001",
                "name": "HR Confidential",
                "description": "HR data",
                "created_at": "2026-01-01T00:00:00",
            }
        )
        result = await engine.create_subgraph(
            driver,
            organisation_id=_ORG,
            graph_id="graph-1",
            name="HR Confidential",
            description="HR data",
            created_by="user-a",
        )
        assert result["subgraph_id"] == "sg-001"
        assert result["graph_id"] == "graph-1"
        assert result["name"] == "HR Confidential"


# ── Acceptable-level hierarchy (Phase A backward-compat) ───────────────────


class TestAcceptableLevels:
    def test_level_hierarchy(self) -> None:
        from oraclous_rebac import _ACCEPTABLE_LEVELS

        assert set(_ACCEPTABLE_LEVELS["read"]) == {"read", "write", "admin"}
        assert set(_ACCEPTABLE_LEVELS["write"]) == {"write", "admin"}
        assert set(_ACCEPTABLE_LEVELS["admin"]) == {"admin"}
