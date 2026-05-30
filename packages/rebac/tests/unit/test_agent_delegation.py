"""Agent-as-subject + ``delegated_to`` / ``delegated_by`` + delegation traversal
+ revocation propagation (ORA-35 / R1-C2; lift-tag **Greenfield**, builds on
C1 / [ORA-34]).

**Q1 revision (ratified by solution-architect, comment 10261; bounced via
be-test-reviewer, comment 10269):** the engine's permission check is
**polymorphic** over the principal type, per ADR-013 §3 (Bounds on adapter
logic). The substrate seam's ``AccessRequest`` carries a polymorphic
subject; separate per-subject-type check methods on the engine would force
every future resolver adapter to dispatch on principal type — violating
the thin-adapter commitment.

Concretely:

* The engine exposes a **single** check method, ``check_graph_permission``,
  that accepts a ``subject`` discriminator of shape
  ``{"type": "user" | "agent", "id": "…"}``. The type set is **closed**:
  any other value (``"service-account"``, ``""``, ``None``) is
  **fail-closed** (raises ``ValueError`` — mirrors the unknown-relation
  rejection in the substrate seam and the C1 ``_require_org`` guard).
* The delegation **CRUD** methods stay separate (``delegate_to_agent`` /
  ``revoke_agent_delegation``) — CRUD separation is genuinely a distinct
  concern, and the C1 precedent of separate operations per role concern
  continues to hold. The check method is where unification matters
  precisely because the seam contract calls it polymorphically.

These tests pin the *contract* the engine must grow on top of the C1 surface:

1.  An ``Agent`` is a first-class ReBAC subject — the existing
    ``check_graph_permission`` resolves agent subjects through a
    delegation traversal phase, returning the agent's effective
    (scope-bounded) access (AC#1).
2.  Revoking the delegation invalidates the delegation cache so the **next**
    invocation fails — the T2-M2 stale-relation tolerance applies (AC#2;
    the 0d-harness data-layer side is asserted in
    ``tests/organization_isolation/test_rebac_delegation_org_edges.py``).
3.  Transitive agent→agent delegation is rejected at the engine API boundary
    (AC#3; T2 transitive-escalation mitigation).
4.  Every delegation edge and every query carries ``organisation_id``; cross-
    organisation delegation is structurally impossible (AC#4; T1 + the no-
    cross-org-delegation R5 deferral).

RED until ``backend-implementer`` adds the polymorphic ``subject``
parameter to ``check_graph_permission`` and the new delegation surface to
``oraclous_rebac.ReBACEngine``. The C1 user-side tests (currently calling
``user_id=…``) will need a paired update in the same ``[impl]`` PR — that
is the implementer's concern, not this file's.

Methods are called with keyword arguments so these tests pin the *contract*
(names + ``organisation_id`` scoping + scope discriminator) without pinning
positional order. Edge labels (``DELEGATED_TO``, ``DELEGATED_BY``) are
asserted as substrings of generated Cypher rather than as exact shapes, so
the implementer keeps freedom on edge direction and any helper labels — the
brief names both relations but only one needs to be traversable to satisfy
the AC.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from oraclous_rebac import ReBACEngine

pytestmark = [pytest.mark.unit, pytest.mark.rebac, pytest.mark.security]

_ORG_A = "org-aaaa"
_OTHER_ORG = "org-bbbb"
_BLANK = ["", "   "]


def _agent(id: str = "agent-1") -> dict:
    """The polymorphic-subject literal for an agent principal."""
    return {"type": "agent", "id": id}


# ── Mock plumbing (mirrors test_rebac_engine.py exactly) ───────────────────


def _null_redis():
    redis = AsyncMock()
    redis.get.return_value = None  # cache miss
    redis.set.return_value = True
    redis.delete.return_value = 1
    return redis


def _make_driver(single_return=None, iter_return=None):
    """Mock async Neo4j driver — ``driver.session()`` returns an async CM."""
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


def _delegation_session(*, authorized: bool):
    """A session whose delegation-traversal query returns the given outcome.

    The traversal phase is asserted at one logical query (``authorized`` →
    a single ``{"authorized": bool}`` record); the implementer is free to
    issue more sub-queries — additional ``session.run`` calls drain to the
    same result mock.
    """
    result = AsyncMock()
    result.single = AsyncMock(return_value={"authorized": authorized})
    session = AsyncMock()
    session.run = AsyncMock(return_value=result)
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


def _queries_of(session) -> list[str]:
    return [call[0][0] for call in session.run.call_args_list]


# ── 1. Agent delegation API surface ────────────────────────────────────────


class TestDelegationApiSurface:
    """The engine grows two delegation-CRUD methods (``delegate_to_agent`` /
    ``revoke_agent_delegation``) and extends ``check_graph_permission`` to
    accept a polymorphic ``subject`` discriminator. Names + signature are
    part of the contract; positional vs keyword call shape is not (we
    always use kw).
    """

    def test_engine_exposes_delegate_to_agent(self) -> None:
        assert callable(getattr(ReBACEngine, "delegate_to_agent", None)), (
            "ReBACEngine.delegate_to_agent must exist (AC#1)"
        )

    def test_engine_exposes_revoke_agent_delegation(self) -> None:
        assert callable(getattr(ReBACEngine, "revoke_agent_delegation", None)), (
            "ReBACEngine.revoke_agent_delegation must exist (AC#2)"
        )

    def test_check_graph_permission_accepts_subject_kwarg(self) -> None:
        """The existing ``check_graph_permission`` is extended with a
        ``subject`` kwarg (the polymorphic discriminator, per ADR-013 §3).

        Asserted by signature introspection rather than by call — a missing
        parameter is unambiguous in the signature object and doesn't
        require constructing a driver mock just to fail. RED until the
        implementer adds ``subject`` to ``check_graph_permission``'s
        signature.
        """
        sig = inspect.signature(ReBACEngine.check_graph_permission)
        assert "subject" in sig.parameters, (
            "ReBACEngine.check_graph_permission must accept a `subject` "
            "kwarg (polymorphic principal type per ADR-013 §3); current "
            f"signature: {sig}"
        )


# ── 2. organisation_id is required everywhere (AC#4, T1) ───────────────────


class TestOrganisationIdRequired:
    @pytest.mark.parametrize("blank", _BLANK)
    async def test_delegate_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.delegate_to_agent(
                driver,
                organisation_id=blank,
                member_user_id="member-a",
                agent_id="agent-1",
                graph_id="graph-1",
                scope="graph",
                granted_by="admin",
            )

    @pytest.mark.parametrize("blank", _BLANK)
    async def test_revoke_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.revoke_agent_delegation(
                driver,
                organisation_id=blank,
                member_user_id="member-a",
                agent_id="agent-1",
                graph_id="graph-1",
                scope="graph",
            )

    @pytest.mark.parametrize("blank", _BLANK)
    async def test_check_rejects_blank_organisation_id(self, blank: str) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=blank,
                subject=_agent(),
                graph_id="graph-1",
                required_level="read",
            )


# ── 3. graph_id is required everywhere (legacy invariant lifted) ──────────


class TestGraphIdRequired:
    async def test_delegate_rejects_blank_graph_id(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.delegate_to_agent(
                driver,
                organisation_id=_ORG_A,
                member_user_id="member-a",
                agent_id="agent-1",
                graph_id="",
                scope="graph",
                granted_by="admin",
            )

    async def test_revoke_rejects_blank_graph_id(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.revoke_agent_delegation(
                driver,
                organisation_id=_ORG_A,
                member_user_id="member-a",
                agent_id="agent-1",
                graph_id="",
                scope="graph",
            )

    async def test_check_rejects_blank_graph_id(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=_ORG_A,
                subject=_agent(),
                graph_id="",
                required_level="read",
            )


# ── 4. organisation_id is bound into every query (AC#4) ───────────────────


class TestOrganisationIdBoundToQueries:
    async def test_delegate_binds_organisation_id(self) -> None:
        engine = _engine()
        driver, session, _ = _make_driver(single_return=None)
        await engine.delegate_to_agent(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
            granted_by="admin",
        )
        assert session.run.called, "delegate_to_agent must issue at least one query"
        for params in _params_of(session):
            assert params.get("organisation_id") == _ORG_A, (
                f"delegate query missing organisation_id binding: {params}"
            )

    async def test_revoke_binds_organisation_id(self) -> None:
        engine = _engine()
        driver, session, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_agent_delegation(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
        )
        assert any(p.get("organisation_id") == _ORG_A for p in _params_of(session)), (
            "revoke_agent_delegation must bind organisation_id"
        )

    async def test_check_binds_organisation_id_on_every_query(self) -> None:
        engine = _engine()
        driver, session = _delegation_session(authorized=False)
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        all_params = _params_of(session)
        assert all_params, "check_graph_permission issued no queries"
        for params in all_params:
            assert params.get("organisation_id") == _ORG_A, (
                f"check query missing organisation_id binding: {params}"
            )


# ── 5. Cross-organisation isolation (T1) ──────────────────────────────────


class TestCrossOrgIsolation:
    async def test_delegate_never_leaks_another_org_id(self) -> None:
        engine = _engine()
        driver, session, _ = _make_driver(single_return=None)
        await engine.delegate_to_agent(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
            granted_by="admin",
        )
        for params in _params_of(session):
            assert _OTHER_ORG not in str(params)

    async def test_check_never_leaks_another_org_id(self) -> None:
        engine = _engine()
        driver, session = _delegation_session(authorized=False)
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        for params in _params_of(session):
            assert _OTHER_ORG not in str(params)

    async def test_revoke_never_leaks_another_org_id(self) -> None:
        engine = _engine()
        driver, session, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_agent_delegation(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
        )
        for params in _params_of(session):
            assert _OTHER_ORG not in str(params)


# ── 6. Agent-as-subject — check resolves through the delegation phase ─────


class TestAgentAsSubjectCheckResolvesDelegation:
    async def test_check_returns_true_when_traversal_authorizes(self) -> None:
        """A traversal that finds a valid member→agent delegation grants access."""
        engine = _engine()
        driver, _ = _delegation_session(authorized=True)
        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is True

    async def test_check_returns_false_when_no_delegation_exists(self) -> None:
        """No delegation path → deny."""
        engine = _engine()
        driver, _ = _delegation_session(authorized=False)
        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is False

    async def test_check_query_references_delegation_relation(self) -> None:
        """When the subject is an agent, the traversal Cypher names a
        delegation edge — the brief explicitly names ``delegated_by`` /
        ``delegated_to``. The implementer is free on direction; this test
        asserts at least one of those tokens appears in the generated
        query text.
        """
        engine = _engine()
        driver, session = _delegation_session(authorized=False)
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        queries = _queries_of(session)
        assert queries, "check_graph_permission issued no queries"
        combined = " ".join(queries).upper()
        assert "DELEGATED_TO" in combined or "DELEGATED_BY" in combined, (
            "delegation traversal Cypher must reference a delegated_to / "
            f"delegated_by relation; saw: {combined[:200]}"
        )

    async def test_check_binds_agent_id_as_parameter_not_literal(self) -> None:
        """The agent identifier must be bound as a Cypher *parameter value*
        (Cypher-injection safe — same convention as ``$user_id`` in C1).

        The architect's polymorphic-subject directive does not pin the
        binding *name* — the implementer may use ``$subject_id``, ``$id``,
        ``$agent_id``, or thread the whole subject dict through. The
        contract is: (a) the identifier appears as a parameter *value*, and
        (b) it is *never* interpolated as a literal into the query text.
        """
        engine = _engine()
        driver, session = _delegation_session(authorized=False)
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent("agent-distinct"),
            graph_id="graph-1",
            required_level="read",
        )
        params_list = _params_of(session)
        assert params_list, "check issued no queries"
        # The agent id must appear as a parameter value somewhere — under
        # any binding name. Walk every value in every params dict.
        flat_values = [
            v
            for params in params_list
            for v in (params.values() if isinstance(params, dict) else ())
        ]

        def _contains_agent_id(value: object) -> bool:
            if value == "agent-distinct":
                return True
            if isinstance(value, dict) and value.get("id") == "agent-distinct":
                return True
            if isinstance(value, list):
                return any(_contains_agent_id(v) for v in value)
            return False

        assert any(_contains_agent_id(v) for v in flat_values), (
            "the agent identifier must be bound as a Cypher parameter value "
            "(any binding name — implementer's choice on shape), not "
            f"literal-interpolated; saw: {params_list}"
        )

    async def test_cache_hit_short_circuits_neo4j(self) -> None:
        """A Redis delegation-cache hit bypasses Neo4j (T2-M2 cache rules — the
        same 60s tolerance as the legacy permission cache).
        """
        engine = ReBACEngine()
        redis = _null_redis()
        redis.get.return_value = "1"
        engine._redis = redis
        driver, session, _ = _make_driver()
        allowed = await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is True
        session.run.assert_not_called()


# ── 6b. Subject-type discrimination is closed (fail-closed, per ADR-013) ──


class TestSubjectTypeRejection:
    """The architect's directive (ratification of Q1, comment 10261):
    the ``subject`` discriminator's ``type`` set is **closed** against
    ``{"user", "agent"}``. Any unrecognised value, missing field, or
    malformed shape **fails closed** (raises ``ValueError``) — mirrors the
    unknown-relation rejection pattern in the substrate seam tests and the
    C1 ``_require_org`` guard.

    Why this matters: a silent fallback (e.g. treat unknown type as
    ``"user"`` and run the C1 path) would be a privilege-escalation bug —
    a future principal type could land in production without explicit
    ReBAC support and accidentally inherit user-style traversal.
    """

    async def test_check_rejects_unknown_subject_type(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=_ORG_A,
                subject={"type": "service-account", "id": "sa-1"},
                graph_id="graph-1",
                required_level="read",
            )

    async def test_check_rejects_subject_missing_type(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=_ORG_A,
                subject={"id": "agent-1"},  # no type
                graph_id="graph-1",
                required_level="read",
            )

    async def test_check_rejects_subject_missing_id(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=_ORG_A,
                subject={"type": "agent"},  # no id
                graph_id="graph-1",
                required_level="read",
            )

    async def test_check_rejects_subject_with_blank_id(self) -> None:
        """A blank id under a valid type is the same class of error as a
        blank ``organisation_id``: silently allowing it would let a request
        with no real principal succeed.
        """
        engine = _engine()
        driver, _, _ = _make_driver()
        with pytest.raises(ValueError):
            await engine.check_graph_permission(
                driver,
                organisation_id=_ORG_A,
                subject={"type": "agent", "id": ""},
                graph_id="graph-1",
                required_level="read",
            )


# ── 7. Scope-bounded delegation (graph / subgraph) ────────────────────────


class TestScopeBoundedDelegation:
    async def test_delegate_accepts_graph_scope(self) -> None:
        """``scope="graph"`` is the whole-graph variant — no subgraph_id."""
        engine = _engine()
        driver, session, _ = _make_driver(single_return=None)
        await engine.delegate_to_agent(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
            granted_by="admin",
        )
        params = _params_of(session)
        assert any(p.get("scope") == "graph" for p in params), (
            f"delegate must bind $scope=graph; saw: {params}"
        )

    async def test_delegate_accepts_subgraph_scope_with_subgraph_id(self) -> None:
        """``scope="subgraph"`` narrows to a single subgraph_id."""
        engine = _engine()
        driver, session, _ = _make_driver(single_return=None)
        await engine.delegate_to_agent(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="subgraph",
            subgraph_id="sg-secret",
            granted_by="admin",
        )
        params = _params_of(session)
        assert any(p.get("scope") == "subgraph" for p in params)
        assert any(p.get("subgraph_id") == "sg-secret" for p in params), (
            f"subgraph-scope delegate must bind $subgraph_id; saw: {params}"
        )

    async def test_delegate_rejects_subgraph_scope_without_subgraph_id(self) -> None:
        """A ``subgraph`` scope without a ``subgraph_id`` is a programmer error —
        a silent fallback to graph-scope would be a privilege-escalation bug.
        """
        engine = _engine()
        driver, _, _ = _make_driver(single_return=None)
        with pytest.raises(ValueError):
            await engine.delegate_to_agent(
                driver,
                organisation_id=_ORG_A,
                member_user_id="member-a",
                agent_id="agent-1",
                graph_id="graph-1",
                scope="subgraph",
                subgraph_id=None,
                granted_by="admin",
            )

    async def test_delegate_rejects_unknown_scope(self) -> None:
        """Only ``graph`` and ``subgraph`` are recognised — anything else must
        fail closed (fail-on-unknown is the safer default than fail-open).
        """
        engine = _engine()
        driver, _, _ = _make_driver(single_return=None)
        with pytest.raises(ValueError):
            await engine.delegate_to_agent(
                driver,
                organisation_id=_ORG_A,
                member_user_id="member-a",
                agent_id="agent-1",
                graph_id="graph-1",
                scope="organisation",  # not allowed pre-R5
                granted_by="admin",
            )

    async def test_subgraph_check_threads_subgraph_id(self) -> None:
        """A check at the subgraph level binds ``$subgraph_id`` so the
        traversal can match only delegations narrowed to that subgraph.
        """
        engine = _engine()
        driver, session = _delegation_session(authorized=False)
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
            subgraph_id="sg-secret",
        )
        assert any(p.get("subgraph_id") == "sg-secret" for p in _params_of(session)), (
            "subgraph-level check must bind $subgraph_id"
        )


# ── 8. Transitive agent→agent delegation rejected (AC#3, T2) ──────────────


class TestTransitiveDelegationRejected:
    async def test_delegate_to_agent_api_rejects_agent_delegator(self) -> None:
        """The ``delegate_to_agent`` API only accepts a *member* delegator (the
        parameter is named ``member_user_id``). Calling it with the agent's id
        in that slot — e.g. via an internal misuse — must be refused at the
        engine boundary.

        Engines that simply trust the caller create a transitive-escalation
        path (agent_X delegates further to agent_Y), which T2-M ("transitive
        escalation") forbids. The brief allows an "explicit scope narrowing"
        carve-out; that is **out of scope for C2** (ratified by
        solution-architect, comment 10261: "rejection is T2-safer; if R4
        surfaces a real narrow-scope need it's a future Contract — schema
        adds the relation, no migration"). Any agent-as-delegator call
        must raise.
        """
        engine = _engine()
        driver, _, _ = _make_driver(single_return=None)
        with pytest.raises(ValueError):
            await engine.delegate_to_agent(
                driver,
                organisation_id=_ORG_A,
                member_user_id="agent-X",  # transitive delegator — forbidden
                agent_id="agent-Y",
                graph_id="graph-1",
                scope="graph",
                granted_by="agent-X",
            )

    async def test_check_traversal_only_authorizes_through_user_delegators(self) -> None:
        """At the data-shape level, the traversal Cypher must require the
        delegator be a User-typed node — the test asserts the query text
        contains a ``User`` (or ``__Platform__``) label on the delegator side
        so an Agent-typed delegator cannot satisfy the match.

        This complements the API-level guard above: even if a transitive
        edge somehow lands in the graph (legacy data, manual write), the
        engine's traversal refuses to authorise through it.
        """
        engine = _engine()
        driver, session = _delegation_session(authorized=False)
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        combined = " ".join(_queries_of(session))
        assert combined, "check issued no queries"
        assert ":User" in combined or "__Platform__" in combined, (
            "delegation traversal must require the delegator be a User-typed "
            "node (no Agent→Agent transitive authorisation); query did not "
            f"reference the User label: {combined[:300]}"
        )


# ── 9. Soft-revoke (lifted from C1 — no DETACH/DELETE on the edge) ────────


class TestSoftRevoke:
    async def test_revoke_flips_is_active_not_deletes(self) -> None:
        """Soft-revoke: ``is_active = false``, never DETACH/DELETE — the
        revoked edge is preserved for audit (lifted from C1 / T7).
        """
        engine = _engine()
        driver, session, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_agent_delegation(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
        )
        assert session.run.called, "revoke must issue a query"
        for q in _queries_of(session):
            assert "is_active" in q, f"revoke must set is_active; saw query: {q[:200]}"
            assert "DELETE" not in q.upper(), (
                f"revoke must be soft (no DELETE / DETACH DELETE); saw: {q[:200]}"
            )

    async def test_revoke_returns_count_zero_when_not_found(self) -> None:
        engine = _engine()
        driver, _, _ = _make_driver(single_return={"revoked_count": 0})
        count = await engine.revoke_agent_delegation(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-missing",
            graph_id="graph-1",
            scope="graph",
        )
        assert count == 0


# ── 10. Revocation invalidates the delegation cache (T2-M2, AC#2) ─────────


class TestRevocationInvalidatesCache:
    async def test_revoke_calls_redis_delete_for_org_scoped_key(self) -> None:
        """Revoking the delegation must invalidate the org-scoped cache so
        the **next** check returns the new (denied) state — the T2-M2
        revocation-propagation contract at the cache layer.
        """
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_agent_delegation(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
        )
        assert redis.delete.called, "revoke_agent_delegation must invalidate the delegation cache"
        combined = " ".join(str(c) for c in redis.delete.call_args_list)
        assert _ORG_A in combined, f"invalidation key must be org-scoped; saw: {combined}"
        assert "agent-1" in combined, f"invalidation must target the agent; saw: {combined}"
        assert "graph-1" in combined, f"invalidation must target the graph; saw: {combined}"

    async def test_revoke_invalidation_is_org_isolated(self) -> None:
        """Invalidating one org's delegation cache must not also delete
        another org's keys (would be a cross-org availability bug).
        """
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _, _ = _make_driver(single_return={"revoked_count": 1})
        await engine.revoke_agent_delegation(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
        )
        deleted = " ".join(str(c) for c in redis.delete.call_args_list)
        assert _OTHER_ORG not in deleted, (
            f"revoke invalidation must not touch other orgs' keys; saw: {deleted}"
        )


# ── 11. Grant invalidates the delegation cache too (avoid stale-deny mask) ─


class TestGrantInvalidatesCache:
    async def test_delegate_invalidates_cache(self) -> None:
        """A delegation grant must invalidate any cached deny for this
        (org, agent, graph) — else a freshly granted scope is masked by a
        cached deny for up to the 60s TTL. Lifted from C1 ``grant_role`` →
        ``invalidate_permission_cache``.
        """
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _, _ = _make_driver(single_return=None)
        await engine.delegate_to_agent(
            driver,
            organisation_id=_ORG_A,
            member_user_id="member-a",
            agent_id="agent-1",
            graph_id="graph-1",
            scope="graph",
            granted_by="admin",
        )
        assert redis.delete.called, "delegate_to_agent must invalidate the delegation cache"
        combined = " ".join(str(c) for c in redis.delete.call_args_list)
        assert _ORG_A in combined
        assert "agent-1" in combined


# ── 12. Delegation cache key is org-namespaced ────────────────────────────


class TestDelegationCacheOrgScoped:
    async def test_check_reads_cache_under_org_namespaced_key(self) -> None:
        """The Redis lookup the check performs is namespaced by
        ``organisation_id`` so a cached allow in one org can never satisfy
        a check in another (cross-org cache-poisoning mitigation, lifted
        from C1 ``TestCacheIsOrgScoped``).
        """
        engine = ReBACEngine()
        redis = _null_redis()
        engine._redis = redis
        driver, _ = _delegation_session(authorized=False)
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        get_keys = " ".join(str(c) for c in redis.get.call_args_list)
        assert _ORG_A in get_keys, f"delegation-cache key not org-namespaced; saw: {get_keys}"


# ── 13. Fail-closed on backend error (lifted from C1) ─────────────────────


class TestFailClosed:
    async def test_neo4j_error_during_check_fails_closed(self) -> None:
        """A Neo4j error during the delegation traversal denies access
        (returns False) and never propagates — lifted from C1 fail-closed.
        """
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
            organisation_id=_ORG_A,
            subject=_agent(),
            graph_id="graph-1",
            required_level="read",
        )
        assert allowed is False

    async def test_check_uses_parameterised_queries(self) -> None:
        """An injection string never appears as a literal in the Cypher text
        (lifted from C1 ``test_check_uses_parameterised_queries``).
        """
        engine = _engine()
        driver, session = _delegation_session(authorized=False)
        injection = "agent'; DROP DATABASE neo4j; --"
        await engine.check_graph_permission(
            driver,
            organisation_id=_ORG_A,
            subject=_agent(injection),
            graph_id="graph-1",
            required_level="read",
        )
        for q in _queries_of(session):
            assert injection not in q, (
                "injection string must never appear in delegation-traversal query text"
            )
