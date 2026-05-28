"""Failing tests for the substrate ReBAC access-decision seam (ORA-15, story 0g).

Behavioural reference: legacy ``knowledge-graph-builder/app/services/rebac_service.py``
(``check_graph_permission``), reshaped to the four-layer substrate seam.

These tests pin the fail-closed contract from the Structured Threat Catalogue
T1-M2 ("ReBAC check at every cross-organisation traversal boundary; fail-closed
default on ambiguous resolution") and ADR-006 (organisation_id on every
operation). They describe behaviour, not implementation: the client is given a
relation *resolver* (a test double standing in for the real ReBAC store), and
the tests assert how the client maps a resolution outcome to an allow/deny
decision.

RED until backend-implementer creates ``oraclous_substrate.rebac``.
"""

from __future__ import annotations

import pytest
from oraclous_substrate.rebac import (
    AccessDecision,
    AccessDecisionClient,
    AccessRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.rebac]


_ORG = "org-aaaa"
_SUBJECT = "user-1234"
_RESOURCE = "graph-9999"


def _request(relation: str = "read", organisation_id: str = _ORG) -> AccessRequest:
    return AccessRequest(
        organisation_id=organisation_id,
        subject=_SUBJECT,
        resource=_RESOURCE,
        relation=relation,
    )


class _Resolver:
    """Test double standing in for the real ReBAC store.

    ``resolve`` returns ``True`` (relation present), ``False`` (relation
    definitively absent), or ``None`` (indeterminate / ambiguous). When
    constructed with ``raises`` it raises, simulating a backend error.
    """

    def __init__(
        self,
        *,
        result: bool | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[AccessRequest] = []

    async def resolve(self, request: AccessRequest) -> bool | None:
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return self._result


async def test_check_allows_when_relation_present() -> None:
    """A present relation resolves to an explicit ALLOW (not deny-everything)."""
    client = AccessDecisionClient(resolver=_Resolver(result=True))
    decision = await client.check(_request())
    assert decision.allowed is True


async def test_check_denies_when_relation_absent() -> None:
    """A definitively-absent relation resolves to DENY."""
    client = AccessDecisionClient(resolver=_Resolver(result=False))
    decision = await client.check(_request())
    assert decision.allowed is False


async def test_check_denies_on_ambiguous_resolution() -> None:
    """T1-M2: an ambiguous/indeterminate resolution (None) fails closed → DENY."""
    client = AccessDecisionClient(resolver=_Resolver(result=None))
    decision = await client.check(_request())
    assert decision.allowed is False


async def test_check_denies_when_backend_errors() -> None:
    """A backend/resolver error fails closed → DENY, and does not propagate.

    Mirrors legacy ``check_graph_permission``, which returns ``False`` on a
    Neo4j error rather than raising — fail-closed, never fail-open.
    """
    client = AccessDecisionClient(resolver=_Resolver(raises=RuntimeError("store down")))
    decision = await client.check(_request())
    assert decision.allowed is False


async def test_deny_decision_is_explicit_not_falsy() -> None:
    """A DENY is a deliberate typed decision, not an implicit ``None``/empty.

    Guards against an ambiguous resolution leaking a falsy value that merely
    *reads* as deny but was never an actual decision.
    """
    client = AccessDecisionClient(resolver=_Resolver(result=None))
    decision = await client.check(_request())
    assert isinstance(decision, AccessDecision)
    assert decision.allowed is False
    assert decision.reason  # a non-empty rationale accompanies the denial


@pytest.mark.parametrize("blank", ["", "   "])
async def test_check_rejects_blank_organisation_id(blank: str) -> None:
    """ADR-006: every access decision is parameterised by organisation_id.

    A blank organisation_id is a programming error and must never silently
    yield ALLOW. Mirrors legacy ``if not graph_id: raise ValueError``. The
    rejection may surface at request construction or at ``check`` — either is
    acceptable, so long as it is never an allow.
    """
    client = AccessDecisionClient(resolver=_Resolver(result=True))
    with pytest.raises(ValueError):
        await client.check(_request(organisation_id=blank))


async def test_unknown_relation_fails_closed() -> None:
    """An unrecognised relation must not allow.

    Legacy mapped an unknown required level to the most-restrictive
    permission. Here, an unknown relation the store cannot resolve (``None``)
    fails closed → DENY.
    """
    client = AccessDecisionClient(resolver=_Resolver(result=None))
    decision = await client.check(_request(relation="totally-unknown-relation"))
    assert decision.allowed is False
