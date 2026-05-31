"""ORA-52 — substrate org-scope helper (AC1, ADR-012 §1/§1b).

The substrate access seam must publish ONE canonical source for:

1. The storage property / Cypher parameter name carrying the organisation id
   (``"organisation_id"``).
2. The parameterised Cypher predicate spelling
   (``<alias>.organisation_id = $organisation_id``).

Every module that needs either MUST source it from this helper — never
inline-derive. This pins the ADR-012 §1/§1b drift-prevention contract that
``solution-architect`` flagged on the ORA-18 Code Review: a change to the
property name or the predicate spelling propagates from one place.

These tests fail RED until ``backend-implementer`` adds the helper surface to
``oraclous_substrate.access`` and refactors ``org_scoped_cypher`` /
``scoped_write_node`` to compose it.

Imports of the not-yet-built helper symbols are function-local per
ORA-48 / TST001 — collection succeeds; tests fail at runtime with
``ImportError`` until the paired ``[impl]`` PR lands.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import use_organisation_context

pytestmark = [pytest.mark.unit, pytest.mark.security]


_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _ctx(org: uuid.UUID = _ORG_A) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=org,
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


# ---------------------------------------------------------------------------
# 1. The helper surface itself
# ---------------------------------------------------------------------------


class TestOrgScopeHelperSurface:
    """``oraclous_substrate.access`` exposes the canonical property name and
    the canonical predicate-fragment composer.

    Both are sourced from the same single-source-of-truth so a future change
    to either propagates from one site (ADR-012 §1b drift control).
    """

    def test_organisation_id_property_constant_is_exposed(self) -> None:
        """The canonical storage / parameter name is published as a constant.

        Modules that stamp ``organisation_id`` on a node, or that read it as
        a Cypher parameter, must reference this constant rather than embed
        the string literal ``"organisation_id"``.
        """
        from oraclous_substrate.access import ORGANISATION_ID_PROPERTY

        assert ORGANISATION_ID_PROPERTY == "organisation_id"

    def test_org_scope_predicate_default_alias_is_node(self) -> None:
        """Default alias matches ``org_scoped_cypher``'s default so a caller
        composing the predicate directly produces the same Cypher fragment as
        the seam emits."""
        from oraclous_substrate.access import org_scope_predicate

        assert org_scope_predicate() == "node.organisation_id = $organisation_id"

    def test_org_scope_predicate_alias_is_parameterised(self) -> None:
        """The alias is parameter-driven so a Cypher template that names its
        match variable differently (e.g. ``MATCH (n:X)``) can still compose
        the canonical predicate.
        """
        from oraclous_substrate.access import org_scope_predicate

        assert org_scope_predicate(alias="n") == "n.organisation_id = $organisation_id"
        assert org_scope_predicate(alias="entity") == ("entity.organisation_id = $organisation_id")

    def test_predicate_uses_canonical_property_name_constant(self) -> None:
        """The right-hand parameter name and the left-hand property name BOTH
        come from the same canonical constant — so renaming the constant
        changes both sides in one edit (single source of truth).
        """
        from oraclous_substrate.access import ORGANISATION_ID_PROPERTY, org_scope_predicate

        predicate = org_scope_predicate(alias="x")
        assert f"x.{ORGANISATION_ID_PROPERTY}" in predicate
        assert f"${ORGANISATION_ID_PROPERTY}" in predicate


# ---------------------------------------------------------------------------
# 2. Substrate seam composes the helper — no inline re-derivation
# ---------------------------------------------------------------------------


class TestOrgScopedCypherComposesHelper:
    """``org_scoped_cypher`` MUST compose ``org_scope_predicate`` rather than
    re-derive the predicate string. Verified by patching the helper to return
    a sentinel and asserting the sentinel appears in the rewritten query —
    i.e. ``org_scoped_cypher`` reads the helper live, not its own copy.
    """

    def test_org_scoped_cypher_uses_helper_for_predicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from oraclous_substrate import access

        sentinel = "SENTINEL_node.organisation_id = $organisation_id"
        monkeypatch.setattr(
            access,
            "org_scope_predicate",
            lambda alias="node": sentinel,
        )

        with use_organisation_context(_ctx()):
            rewritten, _params = access.org_scoped_cypher("MATCH (node:Entity) RETURN node")

        assert sentinel in rewritten, (
            "org_scoped_cypher must compose org_scope_predicate; if the sentinel "
            "is missing the predicate was re-derived inline (AC1 drift)."
        )

    def test_org_scoped_cypher_honours_alias_kw_through_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``alias`` kwarg threads through to the helper so the predicate
        binds against the caller's match variable, not a hard-coded one."""
        from oraclous_substrate import access

        seen: dict[str, str] = {}

        def _spy(alias: str = "node") -> str:
            seen["alias"] = alias
            return f"{alias}.organisation_id = $organisation_id"

        monkeypatch.setattr(access, "org_scope_predicate", _spy)

        with use_organisation_context(_ctx()):
            access.org_scoped_cypher("MATCH (n:Entity) RETURN n", alias="n")

        assert seen == {"alias": "n"}


class TestScopedWriteNodeComposesHelper:
    """``scoped_write_node`` MUST stamp the property using
    ``ORGANISATION_ID_PROPERTY`` so a future rename of the canonical property
    name propagates from one site (single source of truth)."""

    def test_scoped_write_node_uses_canonical_property_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from oraclous_substrate import access

        # Re-bind the canonical constant to a sentinel name; if the writer
        # composes the helper, the sentinel appears as the property key.
        sentinel_property = "SENTINEL_organisation_id"
        monkeypatch.setattr(access, "ORGANISATION_ID_PROPERTY", sentinel_property)

        captured: dict[str, object] = {}

        class _FakeDriver:
            def execute_query(self, query: str, **kwargs: object) -> None:
                captured["query"] = query
                captured["props"] = kwargs.get("props")

        with use_organisation_context(_ctx()):
            access.scoped_write_node(
                _FakeDriver(),
                label="Person",
                properties={"name": "Alice"},
            )

        props = captured["props"]
        assert isinstance(props, dict)
        assert sentinel_property in props, (
            "scoped_write_node must stamp via ORGANISATION_ID_PROPERTY; "
            "if the sentinel key is missing the property name was inlined "
            "(AC1 drift)."
        )
        # The caller-supplied properties survive alongside the stamp.
        assert props.get("name") == "Alice"
