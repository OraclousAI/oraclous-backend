"""Federation cross-organisation traversal is mediated by the ReBAC client
(ORA-17 / A2, AC#3).

RED until ``backend-implementer`` adds ``oraclous_substrate.access``.

Reshape (lift-tag **Reshape**) of the legacy fail-closed cross-tenant check in
``knowledge-graph-builder`` ``app/services/federation_service.py::_validate_and_filter``
(and the cross-graph ``app/tasks/federation_tasks.py`` path): instead of the legacy
ownership / ``federatable`` check that raised ``FederationError 403``, a cross-graph
traversal is routed through the substrate ReBAC access-decision client (ORA-15 / 0g),
which fails closed on an absent, ambiguous, or errored decision (ADR-004; Threat
Catalogue T1-M2). The denial must not leak the target resource id (legacy ORA-89
enumeration-prevention invariant).

Subject and ``organisation_id`` for the decision are taken from the bound org-context
(ORA-14 / 0f), never from a request body (ADR-006).
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    use_organisation_context,
)
from oraclous_substrate.access import (
    CrossOrganisationDenied,
    authorise_cross_org_traversal,
)
from oraclous_substrate.rebac import AccessDecisionClient, AccessRequest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.rebac,
    pytest.mark.federation,
]

ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
PRINCIPAL = uuid.UUID("33333333-3333-3333-3333-333333333333")
TARGET = "graph-b-99999"
RELATION = "federate"


def _ctx() -> OrganisationContext:
    return OrganisationContext(
        organisation_id=ORG_A,
        principal_id=PRINCIPAL,
        principal_type=PrincipalType.USER,
    )


class _Resolver:
    """Test double for the real ReBAC store (mirrors
    ``packages/substrate/tests/unit/test_rebac_client.py``).

    ``resolve`` returns ``True`` (relation present), ``False`` (definitively
    absent), or ``None`` (indeterminate / ambiguous); constructed with ``raises``
    it raises, simulating a store error.
    """

    def __init__(self, *, result: bool | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[AccessRequest] = []

    async def resolve(self, request: AccessRequest) -> bool | None:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return self._result


def _client(**kw) -> AccessDecisionClient:
    return AccessDecisionClient(resolver=_Resolver(**kw))


async def test_traversal_allowed_when_relation_present() -> None:
    client = _client(result=True)
    with use_organisation_context(_ctx()):
        # An explicit grant does not raise — the traversal is authorised.
        await authorise_cross_org_traversal(client, resource=TARGET, relation=RELATION)


async def test_traversal_denied_when_relation_absent() -> None:
    client = _client(result=False)
    with use_organisation_context(_ctx()):
        with pytest.raises(CrossOrganisationDenied):
            await authorise_cross_org_traversal(client, resource=TARGET, relation=RELATION)


async def test_traversal_denied_on_ambiguous_resolution() -> None:
    """AC#3 / T1-M2: an ambiguous (``None``) decision fails closed → denied."""
    client = _client(result=None)
    with use_organisation_context(_ctx()):
        with pytest.raises(CrossOrganisationDenied):
            await authorise_cross_org_traversal(client, resource=TARGET, relation=RELATION)


async def test_traversal_denied_on_resolver_error() -> None:
    """A store error denies rather than propagating or allowing (T1-M2)."""
    client = _client(raises=RuntimeError("store down"))
    with use_organisation_context(_ctx()):
        with pytest.raises(CrossOrganisationDenied):
            await authorise_cross_org_traversal(client, resource=TARGET, relation=RELATION)


async def test_traversal_decision_is_org_and_subject_scoped_from_context() -> None:
    """The ``AccessRequest`` sent to the store carries ``organisation_id`` + subject
    taken from the bound context (ADR-006), not from arguments/body."""
    resolver = _Resolver(result=True)
    client = AccessDecisionClient(resolver=resolver)
    with use_organisation_context(_ctx()):
        await authorise_cross_org_traversal(client, resource=TARGET, relation=RELATION)

    assert len(resolver.calls) == 1
    req = resolver.calls[0]
    assert req.organisation_id == str(ORG_A)
    assert req.subject == str(PRINCIPAL)
    assert req.resource == TARGET
    assert req.relation == RELATION


async def test_traversal_denial_does_not_leak_resource_id() -> None:
    """Legacy ORA-89 invariant: the denial must not echo the target resource id
    (prevents resource enumeration via error text)."""
    client = _client(result=None)
    with use_organisation_context(_ctx()):
        with pytest.raises(CrossOrganisationDenied) as exc:
            await authorise_cross_org_traversal(client, resource=TARGET, relation=RELATION)
    assert TARGET not in str(exc.value)


async def test_traversal_fails_closed_without_context() -> None:
    """No bound org-context → the decision cannot be formed → it must halt, never
    allow. (Fail-closed either at context resolution or as an explicit denial.)"""
    client = _client(result=True)
    with pytest.raises((MissingOrganisationContextError, CrossOrganisationDenied)):
        await authorise_cross_org_traversal(client, resource=TARGET, relation=RELATION)
