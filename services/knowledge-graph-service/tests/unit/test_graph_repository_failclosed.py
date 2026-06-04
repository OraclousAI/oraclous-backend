"""GraphRepository org-scope is fail-closed (R3.5-P1-S1, ADR-006/ADR-012).

The repository reads the org id from the bound governance context via
`oraclous_substrate.access.enforced_organisation_id()`. With NO context bound, every scoped
operation must raise `MissingOrganisationContextError` — never silently fall back to a default or an
unscoped query. This guards the tenancy seam the dev-auth binder sits behind.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from oraclous_governance import OrganisationContext, PrincipalType
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    use_organisation_context,
)
from oraclous_knowledge_graph_service.repositories.graph_repository import GraphRepository

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000000d5")


def test_org_resolution_fails_closed_when_unbound() -> None:
    repo = GraphRepository(session=MagicMock())
    with pytest.raises(MissingOrganisationContextError):
        repo._org()


def test_org_resolution_returns_bound_org() -> None:
    repo = GraphRepository(session=MagicMock())
    ctx = OrganisationContext(
        organisation_id=_ORG, principal_id=_USER, principal_type=PrincipalType.USER
    )
    with use_organisation_context(ctx):
        assert repo._org() == _ORG
